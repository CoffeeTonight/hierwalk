#!/usr/bin/env python3
"""
Unified driver: build_db → hier_resolve → hier_conn → hier_slang (Ibex-first).

  python3 pyhirewalk.py --target ibex
  python3 pyhirewalk.py --config examples/ibex/run_ibex.json --steps all

Steps can be selected: db, resolve, conn, slang, all.
Logs timings; aborts step on hard failure; continues optional slang if missing files.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
# This file is named pyhirewalk.py; if project root is on sys.path first,
# `import pyhirewalk` binds to this script instead of src/pyhirewalk package.
_root_s = str(_ROOT)
while _root_s in sys.path:
    sys.path.remove(_root_s)
# also drop empty '' which is script cwd (same root when launched from project)
if "" in sys.path:
    sys.path.remove("")
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))
# hier_resolve.py / hier_slang.py live at project root
sys.path.append(_root_s)

_TOOL = "pyhirewalk"


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str, t0: float) -> None:
    print(
        f"[{_TOOL}] [{_ts()}] (+{time.perf_counter() - t0:8.3f}s) {msg}",
        file=sys.stderr,
    )


def _default_ibex_config() -> Path:
    return _ROOT / "examples" / "ibex" / "run_ibex.json"


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_filelist(path: Path) -> List[str]:
    out: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("+") or line.startswith("-"):
            continue
        out.append(line)
    return out


def step_build_db(cfg_path: Path, work: Path, t0: float) -> Path:
    from pyhirewalk.index.build_db import build_essential_db
    from pyhirewalk.run_config import load_run_config

    cfg = load_run_config(cfg_path)
    fl = cfg.filelist
    if fl is None or not Path(fl).is_file():
        raise SystemExit(f"build_db: missing filelist in config: {fl}")
    db_out = cfg.db_path or (work / "ibex.sqlite")
    modules_json = None
    if cfg.raw and isinstance(cfg.raw.get("build_db"), dict):
        modules_json = cfg.raw["build_db"].get("modules_json")
    if not modules_json:
        modules_json = work / "ibex.modules.json"
    mode = "fast"
    if cfg.raw and isinstance(cfg.raw.get("build_db"), dict):
        mode = cfg.raw["build_db"].get("mode") or "fast"

    _log(f"STEP build_db mode={mode} filelist={fl}", t0)

    def progress(m: str) -> None:
        _log(f"  db: {m}", t0)

    res = build_essential_db(
        fl,
        db_out,
        index_cwd=cfg.index_cwd,
        top=cfg.top or None,
        extra_defines=cfg.defines,
        env=cfg.env,
        work_dir=work,
        mode=mode,
        modules_json=modules_json,
        write_sqlite=True,
        on_progress=progress,
    )
    mj = Path(res.modules_json) if res.modules_json else Path(modules_json)
    _log(
        f"STEP build_db DONE modules={res.n_unique_module_names} "
        f"files={res.n_files} total={res.timings.get('total', 0):.3f}s "
        f"map={mj}",
        t0,
    )
    if res.timings:
        for k, v in sorted(res.timings.items()):
            _log(f"  timing db.{k}={v:.3f}s", t0)
    return mj


def step_resolve(
    cfg_path: Path, map_path: Path, out: Path, t0: float
) -> Path:
    from hier_resolve import HierResolveApp  # type: ignore
    from pyhirewalk.run_config import load_hier_resolve_inputs

    paths, defines, _cfg_map = load_hier_resolve_inputs(cfg_path)
    _log(
        f"STEP hier_resolve map={map_path} n_paths={len(paths)} -> {out}",
        t0,
    )
    if not paths:
        raise SystemExit("hier_resolve: no hierarchies from run_conn_check.checks a/b")
    rc = HierResolveApp(
        map_path,
        list(paths),
        out=out,
        defines=defines,
    ).run()
    if rc != 0:
        raise SystemExit(f"hier_resolve failed rc={rc}")
    _log(f"STEP hier_resolve DONE wrote {out}", t0)
    return out


def step_conn(
    cfg_path: Path,
    map_path: Path,
    resolve: Path,
    out: Path,
    t0: float,
    *,
    max_hops: int = 32,
    max_nodes: int = 2000,
) -> Path:
    from pyhirewalk.conn.app import HierConnApp

    _log(
        f"STEP hier_conn resolve={resolve} -> {out} "
        f"max_hops={max_hops} max_nodes={max_nodes}",
        t0,
    )
    rc = HierConnApp(
        config=cfg_path,
        map_path=map_path,
        resolve_json=resolve,
        out=out,
        max_hops=max_hops,
        max_nodes=max_nodes,
    ).run()
    if rc != 0:
        raise SystemExit(f"hier_conn failed rc={rc}")
    _log(f"STEP hier_conn DONE wrote {out}", t0)
    return out


def step_slang(
    cfg_path: Path,
    map_path: Path,
    resolve: Path,
    out: Path,
    *,
    filelist: Path,
    top: str,
    include_dirs: List[str],
    t0: float,
) -> Path:
    """Legacy hybrid slang path (map+resolve). Prefer step_pyslang."""
    from pyhirewalk.conn.slang import HierSlangApp

    files = _read_filelist(filelist)
    _log(
        f"STEP hier_slang (legacy) top={top} n_files={len(files)} -> {out}",
        t0,
    )
    rc = HierSlangApp(
        config=cfg_path,
        map_path=map_path,
        resolve_json=resolve,
        out=out,
        files=files,
        top=top,
        include_dirs=include_dirs,
    ).run()
    if rc != 0:
        raise SystemExit(f"hier_slang failed rc={rc}")
    _log(f"STEP hier_slang DONE wrote {out}", t0)
    return out


def step_pyslang(cfg_path: Path, out: Path, t0: float) -> Path:
    """pyslang-only COI: env+defines+filelist+top from config."""
    from pyhirewalk.conn.pyslang_app import HierPyslangApp
    from pyhirewalk.run_config import load_run_config

    cfg = load_run_config(cfg_path)
    _log(f"STEP hier_pyslang config={cfg_path} -> {out}", t0)
    rc = HierPyslangApp(
        config=cfg_path,
        out=out,
        cone_walk=True,
        cone_files=bool(cfg.modules_json),
        modules_json=cfg.modules_json,
    ).run()
    if rc not in (0, 1):
        raise SystemExit(f"hier_pyslang failed rc={rc}")
    _log(f"STEP hier_pyslang DONE wrote {out} rc={rc}", t0)
    return out


def _summarize(path: Path, label: str, t0: float) -> None:
    if not path.is_file():
        _log(f"review {label}: missing {path}", t0)
        return
    doc = _load_json(path)
    checks = doc.get("checks") or []
    meta = doc.get("meta") or {}
    stats = meta.get("stats") or {}
    _log(
        f"review {label}: n_checks={len(checks)} "
        f"n_pairs={stats.get('n_pairs')} total_sec={stats.get('total_sec')}",
        t0,
    )
    for ch in checks:
        n_p = len(ch.get("pairs") or [])
        n_u = len(ch.get("unconnected") or [])
        reasons = {}
        for u in ch.get("unconnected") or []:
            r = u.get("reason") or "?"
            reasons[r] = reasons.get(r, 0) + 1
        eng = ch.get("engine") or "conn"
        _log(
            f"  [{eng}] {ch.get('id')}: pairs={n_p} unconnected={n_u} "
            f"reasons={reasons}",
            t0,
        )
        for pr in (ch.get("pairs") or [])[:2]:
            ev = pr.get("evidence") or []
            snip = (ev[0].get("snippet") if ev else "") or ""
            _log(
                f"    pair {pr.get('src')} -> {pr.get('dst')} "
                f"ev_n={len(ev)} snip={snip[:80]!r}",
                t0,
            )


def run_pipeline(
    *,
    config: Path,
    steps: Sequence[str],
    work: Optional[Path] = None,
    slang_top: Optional[str] = None,
) -> int:
    t0 = time.perf_counter()
    config = config.resolve()
    if not config.is_file():
        _log(f"ERROR config not found: {config}", t0)
        return 2

    raw = _load_json(config)
    work = Path(work) if work else Path(raw.get("env", {}).get("WORK") or (config.parent / "work"))
    work.mkdir(parents=True, exist_ok=True)

    map_path = Path(raw.get("modules_json") or (work / "ibex.modules.json"))
    resolve_out = work / "hier_resolve.json"
    conn_out = work / "hier_conn.json"
    slang_out = work / "hier_slang.json"
    filelist = Path(raw["filelist"]) if raw.get("filelist") else None
    top = slang_top or raw.get("top") or "ibex_top"
    # slang prefers a smaller top when possible — ibex_core is enough for core paths
    if slang_top is None and top == "ibex_top":
        # still elab ibex_top if that's the seed root; hierarchical paths start with ibex_top
        slang_elab_top = "ibex_top"
    else:
        slang_elab_top = top

    ibex_root = Path(raw.get("env", {}).get("IBEX_ROOT") or "/tmp/rtl-bench/ibex")
    include_dirs = [
        str(ibex_root / "rtl"),
        str(ibex_root / "vendor/lowrisc_ip/ip/prim/rtl"),
        str(ibex_root / "vendor/lowrisc_ip/ip/prim_generic/rtl"),
    ]

    step_set = set(steps)
    if "all" in step_set:
        # default: db + resolve + regex conn + hier_pyslang (not legacy slang)
        step_set = {"db", "resolve", "conn", "pyslang"}

    _log(f"PIPELINE start config={config} work={work} steps={sorted(step_set)}", t0)

    timings: Dict[str, float] = {}
    pyslang_out = work / "hier_pyslang.json"

    if "db" in step_set:
        t = time.perf_counter()
        map_path = step_build_db(config, work, t0)
        timings["db"] = time.perf_counter() - t

    if "resolve" in step_set:
        t = time.perf_counter()
        if not map_path.is_file():
            raise SystemExit(f"resolve needs modules map: {map_path}")
        step_resolve(config, map_path, resolve_out, t0)
        timings["resolve"] = time.perf_counter() - t

    if "conn" in step_set:
        t = time.perf_counter()
        if not resolve_out.is_file():
            raise SystemExit(f"conn needs resolve json: {resolve_out}")
        step_conn(
            config,
            map_path,
            resolve_out,
            conn_out,
            t0,
            max_hops=32,
            max_nodes=2000,
        )
        timings["conn"] = time.perf_counter() - t
        _summarize(conn_out, "hier_conn", t0)

    if "pyslang" in step_set:
        t = time.perf_counter()
        step_pyslang(config, pyslang_out, t0)
        timings["pyslang"] = time.perf_counter() - t
        _summarize(pyslang_out, "hier_pyslang", t0)

    if "slang" in step_set:
        t = time.perf_counter()
        if filelist is None or not filelist.is_file():
            raise SystemExit("slang needs config filelist")
        if not resolve_out.is_file():
            raise SystemExit(f"slang needs resolve json: {resolve_out}")
        step_slang(
            config,
            map_path,
            resolve_out,
            slang_out,
            filelist=filelist,
            top=slang_elab_top,
            include_dirs=include_dirs,
            t0=t0,
        )
        timings["slang"] = time.perf_counter() - t
        _summarize(slang_out, "hier_slang", t0)

    total = time.perf_counter() - t0
    _log("==== TIMING SUMMARY ====", t0)
    for k, v in timings.items():
        flag = " **SLOW**" if v > 30.0 else ""
        _log(f"  step_{k}={v:.3f}s{flag}", t0)
    _log(f"TOTAL_PYHIREWALK_SEC={total:.3f}", t0)
    print(f"TOTAL_PYHIREWALK_SEC: {total:.3f}", file=sys.stderr)

    # write summary json
    summary = {
        "config": str(config),
        "work": str(work),
        "steps": sorted(step_set),
        "timings_sec": timings,
        "total_sec": round(total, 6),
        "outputs": {
            "modules_json": str(map_path),
            "hier_resolve": str(resolve_out) if resolve_out.is_file() else None,
            "hier_conn": str(conn_out) if conn_out.is_file() else None,
            "hier_pyslang": str(pyslang_out) if pyslang_out.is_file() else None,
            "hier_slang": str(slang_out) if slang_out.is_file() else None,
        },
    }
    sum_path = work / "pyhirewalk_summary.json"
    sum_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    _log(f"wrote {sum_path}", t0)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Unified pyhirewalk: build_db + hier_resolve + hier_conn + hier_slang"
    )
    ap.add_argument(
        "--target",
        choices=["ibex"],
        default=None,
        help="preset target (ibex → examples/ibex/run_ibex.json)",
    )
    ap.add_argument("--config", "-c", type=Path, default=None)
    ap.add_argument(
        "--steps",
        default="all",
        help="comma list: db,resolve,conn,pyslang,slang,all "
        "(all = db+resolve+conn+pyslang; slang = legacy hybrid)",
    )
    ap.add_argument("--work", type=Path, default=None)
    ap.add_argument(
        "--slang-top",
        default=None,
        help="override slang --top (default: config top)",
    )
    args = ap.parse_args(argv)

    if args.config is None:
        if args.target == "ibex" or args.target is None:
            args.config = _default_ibex_config()
        else:
            ap.error("need --config or --target ibex")

    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    return run_pipeline(
        config=args.config,
        steps=steps,
        work=args.work,
        slang_top=args.slang_top,
    )


if __name__ == "__main__":
    raise SystemExit(main())
