#!/usr/bin/env python3
"""
COI expansion from seed group *a* until stop conditions (e.g. FF:2).

Not bi-meet (a↔b). Walks structural graph from a until each frontier either:

  * satisfies the condition (e.g. path crossed ≥ 2 always_ff / proc edges), or
  * has nowhere left to go (exhausted — e.g. only 1 FF on that branch).

Examples:

  python3 coi_until.py --config examples/bench/configs/coi_ladder.json -o work/coi.json

  python3 coi_until.py --config examples/bench/configs/coi_ladder.json \\
      --a coi_top.a_i --until FF:2 --verify

  python3 coi_until.py -c cfg.json --a top.x --a top.y --until FF:3 \\
      --direction fanout --max-nodes 4000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
_root_s = str(_ROOT)
while _root_s in sys.path:
    sys.path.remove(_root_s)
if "" in sys.path:
    sys.path.remove("")
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))
sys.path.append(_root_s)


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str, t0: float) -> None:
    print(
        f"[coi_until] [{_ts()}] (+{time.perf_counter() - t0:8.3f}s) {msg}",
        file=sys.stderr,
    )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="COI from a[] until FF:N (or hop/assign/port) conditions"
    )
    ap.add_argument("--config", "-c", type=Path, required=True)
    ap.add_argument("-o", "--out", type=Path, default=None)
    ap.add_argument(
        "--a",
        action="append",
        default=[],
        dest="seeds",
        help="seed hierarchical path (repeatable); overrides config coi_until.a",
    )
    ap.add_argument(
        "--until",
        default=None,
        help="stop condition e.g. FF:2 (default: config coi_until.until or FF:2)",
    )
    ap.add_argument(
        "--direction",
        choices=("fanout", "fanin", "both"),
        default=None,
        help="fanout=forward COI (default), fanin=backward, both",
    )
    ap.add_argument("--max-hops", type=int, default=None)
    ap.add_argument("--max-nodes", type=int, default=None)
    ap.add_argument(
        "--cone-walk",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="limit graph extract to seed hierarchy prefixes (default on)",
    )
    ap.add_argument(
        "--verify",
        action="store_true",
        help="check config coi_until.expect answer key (exit 1 on fail)",
    )
    ap.add_argument(
        "--filelist",
        type=Path,
        default=None,
        help="override config filelist",
    )
    ap.add_argument("--top", default=None)
    args = ap.parse_args(argv)

    t0 = time.perf_counter()
    _log(f"START config={args.config}", t0)

    from pyhirewalk.run_config import load_run_config
    from pyhirewalk.conn.pyslang_app import (
        apply_config_env,
        build_graph,
        compile_design,
        cone_instance_prefixes,
        expand_env_str,
        load_rtl_sources,
    )
    from pyhirewalk.conn.coi_until import (
        StopCond,
        coi_until,
        verify_expect,
    )

    cfg = load_run_config(args.config)
    raw = cfg.raw or {}
    coi_cfg: Dict[str, Any] = {}
    if isinstance(raw.get("coi_until"), dict):
        coi_cfg = dict(raw["coi_until"])

    env = dict(cfg.env)
    apply_config_env(env, t0=t0)

    seeds: List[str] = list(args.seeds) if args.seeds else []
    if not seeds:
        seeds = [str(x) for x in (coi_cfg.get("a") or [])]
    if not seeds:
        # also allow run_conn_check first check a[] as convenience
        rcc = raw.get("run_conn_check") or {}
        checks = rcc.get("checks") or []
        if checks and checks[0].get("a"):
            seeds = [str(x) for x in checks[0]["a"]]
    if not seeds:
        _log("ERROR: no seeds — pass --a PATH or config coi_until.a", t0)
        return 2

    until_s = args.until or coi_cfg.get("until") or "FF:2"
    try:
        until = StopCond.parse(str(until_s))
    except ValueError as e:
        _log(f"ERROR: {e}", t0)
        return 2

    direction = args.direction or str(coi_cfg.get("direction") or "fanout")
    max_hops = int(
        args.max_hops
        if args.max_hops is not None
        else coi_cfg.get("max_hops") or 64
    )
    max_nodes = int(
        args.max_nodes
        if args.max_nodes is not None
        else coi_cfg.get("max_nodes") or 8000
    )

    top = args.top or cfg.top or ""
    if not top:
        _log("ERROR: top module required", t0)
        return 2

    fl = args.filelist or cfg.filelist
    if fl is None:
        _log("ERROR: no filelist", t0)
        return 2
    fl_path = Path(fl)
    if not fl_path.is_file():
        _log(f"ERROR: filelist not found: {fl_path}", t0)
        return 2

    files, fl_incdirs, fl_defines, fl_errors = load_rtl_sources(
        fl_path, env=env, index_cwd=cfg.index_cwd, t0=t0
    )
    defines = dict(cfg.defines)
    for k, v in fl_defines.items():
        defines.setdefault(k, v)
    if fl_errors:
        for e in fl_errors[:6]:
            _log(f"filelist warn: {e}", t0)
    if not files:
        _log("ERROR: empty filelist after expand", t0)
        return 2

    includes: List[str] = list(fl_incdirs)
    if cfg.index_cwd:
        includes.append(str(Path(cfg.index_cwd).resolve()))
    seen_i: Set[str] = set()
    uniq_inc: List[str] = []
    for i in includes:
        i = expand_env_str(i, env)
        if i not in seen_i:
            seen_i.add(i)
            uniq_inc.append(i)

    parameters: Dict[str, str] = {}
    raw_params = raw.get("parameters")
    if isinstance(raw_params, dict):
        parameters = {str(k): str(v) for k, v in raw_params.items()}

    _log(
        f"seeds={seeds} until={until.kind}:{until.limit} "
        f"dir={direction} top={top} n_rtl={len(files)}",
        t0,
    )

    t_c0 = time.perf_counter()
    try:
        comp, root, diags, fatal = compile_design(
            files=files,
            top=top,
            defines=defines,
            includes=uniq_inc,
            t0=t0,
            parameters=parameters or None,
        )
    except Exception as e:
        _log(f"ERROR compile: {e}", t0)
        return 3
    t_comp = time.perf_counter() - t_c0
    sm = comp.sourceManager
    _log(
        f"compile done diags={len(diags) if diags is not None else '?'} "
        f"fatal={fatal} ({t_comp:.3f}s)",
        t0,
    )
    if fatal:
        _log("ERROR: fatal compile diagnostics", t0)
        return 3

    # cone from seeds only (synthetic check for prefix helper)
    cone_prefs = None
    if args.cone_walk:
        cone_prefs = cone_instance_prefixes([{"a": seeds, "b": []}])
        _log(f"cone_walk prefixes={sorted(cone_prefs)[:8]}", t0)

    t_g0 = time.perf_counter()
    g = build_graph(root, sm, t0=t0, cone_prefs=cone_prefs)
    t_graph = time.perf_counter() - t_g0
    _log(
        f"graph fwd={len(g.forward)} assign={g.n_assign} port={g.n_port} "
        f"proc={g.n_proc} ({t_graph:.3f}s)",
        t0,
    )

    t_s0 = time.perf_counter()
    result = coi_until(
        g,
        seeds,
        until=until,
        direction=direction,
        max_nodes=max_nodes,
        max_hops=max_hops,
    )
    t_search = time.perf_counter() - t_s0
    result.timings_sec = {
        "compile": round(t_comp, 6),
        "graph": round(t_graph, 6),
        "search": round(t_search, 6),
        "total": round(time.perf_counter() - t0, 6),
    }

    _log(
        f"COI n={result.stats['n_coi']} satisfied={result.stats['n_satisfied']} "
        f"exhausted={result.stats['n_exhausted']} "
        f"truncated={result.stats['n_truncated']} "
        f"ff_edges={result.stats['n_ff_edges_touched']} "
        f"search={t_search:.4f}s",
        t0,
    )
    if result.unresolved_seeds:
        _log(f"unresolved seeds: {result.unresolved_seeds}", t0)

    # concise path dump
    for row in result.to_json()["satisfied"][:16]:
        _log(
            f"  SAT ff={row['counters']['ff']} hop={row['counters']['hop']} "
            f"{row['net']} ev={row['evidence_len']}",
            t0,
        )
    for row in result.to_json()["exhausted"][:12]:
        _log(
            f"  EXH ff={row['counters']['ff']} hop={row['counters']['hop']} "
            f"{row['net']} ev={row['evidence_len']}",
            t0,
        )

    verify_doc: Optional[Dict[str, Any]] = None
    expect = coi_cfg.get("expect")
    # Only when --verify: answer-key is for the config's default until (FF:2).
    if args.verify:
        if not expect:
            _log("WARN: --verify but no coi_until.expect in config", t0)
            verify_doc = {"pass": False, "failures": ["no expect block"]}
        else:
            # Guard: if CLI overrides until to something else, note mismatch
            cfg_until = str(coi_cfg.get("until") or "FF:2")
            if args.until and StopCond.parse(args.until).to_dict()[
                "spec"
            ] != StopCond.parse(cfg_until).to_dict()["spec"]:
                _log(
                    f"WARN: --until {args.until} != config until {cfg_until}; "
                    "expect key is for config until",
                    t0,
                )
            verify_doc = verify_expect(result, expect)
            mark = "PASS" if verify_doc["pass"] else "FAIL"
            _log(
                f"verify {mark} failures={len(verify_doc.get('failures') or [])}",
                t0,
            )
            for f in (verify_doc.get("failures") or [])[:12]:
                _log(f"  ! {f}", t0)

    out_doc: Dict[str, Any] = {
        "meta": {
            "tool": "coi_until",
            "config": str(args.config),
            "top": top,
            "filelist": str(fl_path),
            "seeds": seeds,
            "until": until.to_dict(),
            "direction": direction,
            "n_diags": len(diags) if diags is not None else None,
            "fatal": fatal,
            "graph": {
                "fwd": len(g.forward),
                "n_assign": g.n_assign,
                "n_port": g.n_port,
                "n_proc": g.n_proc,
            },
            "timings_sec": result.timings_sec,
            "stats": result.stats,
        },
        "coi": result.to_json(),
        "verify": verify_doc,
    }

    out_path = args.out
    if out_path is None:
        out_path = Path(coi_cfg.get("out") or "coi_until.json")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_doc, indent=2), encoding="utf-8")
    _log(f"wrote {out_path} total={result.timings_sec['total']:.3f}s", t0)

    if verify_doc is not None and not verify_doc.get("pass"):
        return 1
    if result.unresolved_seeds:
        _log(
            f"ERROR: unresolved seeds ({len(result.unresolved_seeds)}): "
            f"{result.unresolved_seeds}",
            t0,
        )
        return 1
    if not result.nodes:
        _log("ERROR: empty COI (no resolved nets)", t0)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
