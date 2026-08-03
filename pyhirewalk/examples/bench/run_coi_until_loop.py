#!/usr/bin/env python3
"""
Loop-engineering harness for coi_until.

Goal cycle per case:
  1) answer key (expect) is in config  — write before trusting tool output
  2) run coi_until
  3) score pass/fail + timings
  4) on fail: inspect log/json, fix engine or tighten golden, re-run

  python3 examples/bench/run_coi_until_loop.py
  python3 examples/bench/run_coi_until_loop.py --only coi_ladder,coi_zigzag
  python3 examples/bench/run_coi_until_loop.py --list
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_BENCH = Path(__file__).resolve().parent
_ROOT = _BENCH.parents[1]
_CONFIGS = _BENCH / "configs"
_WORK = _BENCH / "work"
_LOGS = _BENCH / "logs"


def _load(p: Path) -> Dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8"))


def discover() -> List[Path]:
    out = []
    for p in sorted(_CONFIGS.glob("*.json")):
        doc = _load(p)
        if "coi_until" in doc or "coi_until_cases" in doc:
            out.append(p)
    return out


def expand_cases(cfg_path: Path) -> List[Dict[str, Any]]:
    """
    Each case is one (seeds, until, direction, expect) run.

    Config shapes:
      coi_until: { a, until, expect, ... }           → single case
      coi_until_cases: [ {id, a?, until, expect}, … ] → multi (inherits base coi_until)
    """
    doc = _load(cfg_path)
    base = dict(doc.get("coi_until") or {})
    cases_raw = doc.get("coi_until_cases")
    if not cases_raw:
        cid = base.get("id") or f"{cfg_path.stem}__{base.get('until') or 'FF:2'}"
        return [
            {
                "id": str(cid),
                "a": list(base.get("a") or []),
                "until": str(base.get("until") or "FF:2"),
                "direction": str(base.get("direction") or "fanout"),
                "max_hops": base.get("max_hops"),
                "max_nodes": base.get("max_nodes"),
                "expect": base.get("expect"),
            }
        ]
    cases: List[Dict[str, Any]] = []
    for raw in cases_raw:
        c = dict(base)
        c.update(raw or {})
        cid = c.get("id") or f"{cfg_path.stem}__{c.get('until')}"
        cases.append(
            {
                "id": str(cid),
                "a": list(c.get("a") or base.get("a") or []),
                "until": str(c.get("until") or "FF:2"),
                "direction": str(c.get("direction") or base.get("direction") or "fanout"),
                "max_hops": c.get("max_hops", base.get("max_hops")),
                "max_nodes": c.get("max_nodes", base.get("max_nodes")),
                "expect": c.get("expect") or base.get("expect"),
            }
        )
    return cases


def run_case(cfg_path: Path, case: Dict[str, Any]) -> Dict[str, Any]:
    _WORK.mkdir(parents=True, exist_ok=True)
    _LOGS.mkdir(parents=True, exist_ok=True)
    cid = case["id"]
    out_json = _WORK / f"{cid}.coi_until.json"
    log_path = _LOGS / f"{cid}.coi_until.log"

    # Write a ephemeral config so expect matches this case's until
    ephemeral = _WORK / f"_ephemeral_{cid}.json"
    base_doc = _load(cfg_path)
    coi = dict(base_doc.get("coi_until") or {})
    coi["a"] = case["a"]
    coi["until"] = case["until"]
    coi["direction"] = case["direction"]
    if case.get("max_hops") is not None:
        coi["max_hops"] = case["max_hops"]
    if case.get("max_nodes") is not None:
        coi["max_nodes"] = case["max_nodes"]
    if case.get("expect") is not None:
        coi["expect"] = case["expect"]
    else:
        coi.pop("expect", None)
    coi["out"] = str(out_json)
    base_doc["coi_until"] = coi
    base_doc.pop("coi_until_cases", None)
    ephemeral.write_text(json.dumps(base_doc, indent=2), encoding="utf-8")

    cmd = [
        sys.executable,
        str(_ROOT / "coi_until.py"),
        "--config",
        str(ephemeral),
        "-o",
        str(out_json),
    ]
    if case.get("expect"):
        cmd.append("--verify")

    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, cwd=str(_ROOT), capture_output=True, text=True)
    finally:
        try:
            ephemeral.unlink(missing_ok=True)  # type: ignore[arg-type]
        except TypeError:
            # py<3.8
            if ephemeral.is_file():
                ephemeral.unlink()
        except OSError:
            pass
    elapsed = time.perf_counter() - t0
    log_path.write_text(
        f"$ {' '.join(cmd)}\n\n--- stdout ---\n{proc.stdout}\n\n--- stderr ---\n{proc.stderr}\n",
        encoding="utf-8",
    )

    row: Dict[str, Any] = {
        "id": cid,
        "config": str(cfg_path),
        "until": case["until"],
        "a": case["a"],
        "rc": proc.returncode,
        "elapsed_sec": round(elapsed, 3),
        "out": str(out_json),
        "log": str(log_path),
        "pass": False,
        "verify": None,
        "stats": None,
        "timings_sec": None,
        "error": None,
    }
    if not out_json.is_file():
        row["error"] = f"no output rc={proc.returncode}\n{(proc.stderr or '')[-1500:]}"
        return row
    try:
        doc = _load(out_json)
    except Exception as e:
        row["error"] = f"bad json: {e}"
        return row
    meta = doc.get("meta") or {}
    coi = doc.get("coi") or {}
    row["timings_sec"] = meta.get("timings_sec") or coi.get("timings_sec")
    row["stats"] = coi.get("stats") or meta.get("stats")
    ver = doc.get("verify")
    row["verify"] = ver
    if case.get("expect"):
        row["pass"] = bool(ver and ver.get("pass")) and proc.returncode == 0
    else:
        # smoke: resolved seeds + non-empty or intentional empty exhaust
        unresolved = (coi.get("unresolved_seeds") or [])
        row["pass"] = proc.returncode == 0 and not unresolved
    if not row["pass"] and not row["error"]:
        fails = (ver or {}).get("failures") if ver else None
        row["error"] = fails or f"rc={proc.returncode}"
    return row


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="coi_until loop harness")
    ap.add_argument("--only", default="", help="comma config stems")
    ap.add_argument("--list", action="store_true")
    ap.add_argument(
        "-o",
        type=Path,
        default=_WORK / "coi_until_loop_summary.json",
    )
    args = ap.parse_args(argv)

    configs = discover()
    if args.only:
        only = {x.strip() for x in args.only.split(",") if x.strip()}
        configs = [c for c in configs if c.stem in only]
    if args.list:
        for c in configs:
            cases = expand_cases(c)
            print(f"{c.stem}: {len(cases)} cases — {[x['id'] for x in cases]}")
        return 0
    if not configs:
        print("No coi_until configs found", file=sys.stderr)
        return 2

    summary: Dict[str, Any] = {
        "n_configs": len(configs),
        "n_pass": 0,
        "n_fail": 0,
        "results": [],
    }
    print(f"[coi-loop] configs={len(configs)} root={_ROOT}", file=sys.stderr)
    for cfg in configs:
        cases = expand_cases(cfg)
        print(f"[coi-loop] CONFIG {cfg.stem} cases={len(cases)}", file=sys.stderr)
        for case in cases:
            print(
                f"[coi-loop]   RUN {case['id']} until={case['until']} …",
                file=sys.stderr,
            )
            r = run_case(cfg, case)
            summary["results"].append(r)
            mark = "PASS" if r["pass"] else "FAIL"
            if r["pass"]:
                summary["n_pass"] += 1
            else:
                summary["n_fail"] += 1
            st = r.get("stats") or {}
            tm = r.get("timings_sec") or {}
            print(
                f"[coi-loop]   {mark} {r['id']}  {r['elapsed_sec']}s  "
                f"coi={st.get('n_coi')} sat={st.get('n_satisfied')} "
                f"exh={st.get('n_exhausted')} "
                f"search={tm.get('search')}s total={tm.get('total')}s",
                file=sys.stderr,
            )
            if not r["pass"]:
                err = r.get("error")
                if isinstance(err, list):
                    for e in err[:6]:
                        print(f"[coi-loop]     ! {e}", file=sys.stderr)
                elif err:
                    print(f"[coi-loop]     ! {err}", file=sys.stderr)

    args.o.parent.mkdir(parents=True, exist_ok=True)
    args.o.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"[coi-loop] wrote {args.o}", file=sys.stderr)
    print(
        f"[coi-loop] TOTAL pass={summary['n_pass']} fail={summary['n_fail']}",
        file=sys.stderr,
    )
    return 0 if summary["n_fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
