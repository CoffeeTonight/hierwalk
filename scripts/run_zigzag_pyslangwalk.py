#!/usr/bin/env python3
"""Run zigzag connect suite with connect_phase=pyslangwalk; write summary report."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hierwalk.connect.shared.request import (  # noqa: E402
    ConnectivityRequest,
    load_connect_request,
)
from hierwalk.filelist import parse_filelist  # noqa: E402
from hierwalk.path_walk import run_path_walk_connect  # noqa: E402
from hierwalk.zigzag_torture_gen import SUITE_CONN_NEGATIVE_IDS  # noqa: E402


def main() -> int:
    zz = Path("/home/user/Desktop/hgrep_demo/.zz_verify").resolve()
    work = zz / ".db_zz_pyslangwalk"
    work.mkdir(parents=True, exist_ok=True)

    base = load_connect_request(zz / "zz_torture.connect.json")
    fl = parse_filelist(
        str(zz / "filelist.f"),
        index_cwd=str(zz),
        extra_defines=dict(base.defines) or None,
    )
    req = ConnectivityRequest(
        checks=base.checks,
        top=base.top,
        defines=dict(base.defines),
        include_ff=True,
    )

    log_path = work / "pyslangwalk.hier-walk.log"
    logs: list[str] = []

    def on_emit(msg: str) -> None:
        logs.append(msg)
        print(msg, flush=True)

    print(
        f"pyslangwalk zigzag: checks={len(req.checks)} sources={len(fl.source_files)} "
        f"top={req.top} work={work}",
        flush=True,
    )
    t0 = time.perf_counter()
    batch, _index, _state = run_path_walk_connect(
        req,
        fl,
        top=req.top,
        extra_defines=dict(req.defines),
        no_cache=False,
        connect_phase="pyslangwalk",
        connect_output_dir=work,
        connect_output_name="conn_pyslangwalk.tsv",
        refresh_cache=False,
        on_progress=on_emit,
        trace_log_path=log_path,
    )
    elapsed = time.perf_counter() - t0

    rows = []
    must_ok = must_fail = neg_ok = neg_fp = 0
    for r in batch.results:
        cid = r.check_id or ""
        ok = bool(r.connected)
        if r.sub_results:
            ok = all(sr.connected for sr in r.sub_results)
        is_neg = cid in SUITE_CONN_NEGATIVE_IDS
        if is_neg:
            if ok:
                neg_fp += 1
                verdict = "neg_false_positive"
            else:
                neg_ok += 1
                verdict = "neg_ok"
        else:
            if ok:
                must_ok += 1
                verdict = "must_ok"
            else:
                must_fail += 1
                verdict = "must_fail"
        rows.append(
            {
                "check_id": cid,
                "connected": ok,
                "verdict": verdict,
                "mode": getattr(r, "mode", ""),
                "note": r.note or "",
                "errors": list(r.errors or []),
                "endpoint_a": r.endpoint_a.spec if r.endpoint_a else "",
                "endpoint_b": r.endpoint_b.spec if r.endpoint_b else "",
            }
        )

    modes = {}
    for r in rows:
        m = r.get("mode") or ""
        modes[m] = modes.get(m, 0) + 1
    summary = {
        "method": "pyslangwalk hierarchy + scoped text-COI",
        "note": (
            "hierarchy via pyslang (module-index files only); "
            "connectivity via path-walk text-COI on scoped RTL"
        ),
        "total": len(rows),
        "connected": sum(1 for x in rows if x["connected"]),
        "must_ok": must_ok,
        "must_fail": must_fail,
        "neg_ok": neg_ok,
        "neg_false_positive": neg_fp,
        "modes": modes,
        "elapsed_sec": round(elapsed, 2),
        "work_dir": str(work),
        "log": str(log_path),
    }
    (work / "pyslangwalk_summary.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    lines = [
        "pyslangwalk zigzag report",
        f"  total:              {summary['total']}",
        f"  connected (text):   {summary['connected']}",
        f"  must_ok:            {must_ok}",
        f"  must_fail:          {must_fail}",
        f"  neg_ok:             {neg_ok}",
        f"  neg_false_positive: {neg_fp}",
        f"  modes:              {summary.get('modes')}",
        f"  elapsed_sec:        {summary['elapsed_sec']}",
        f"  work_dir:           {work}",
        "",
        "  Pipeline: pyslang hierarchy → scoped text-COI",
        "",
    ]
    fails = [r for r in rows if r["verdict"] == "must_fail"]
    if fails:
        lines.append("  must_fail detail:")
        for r in fails[:40]:
            lines.append(
                f"    {r['check_id']}: {r['errors'] or r['note']}"
            )
    negfps = [r for r in rows if r["verdict"] == "neg_false_positive"]
    if negfps:
        lines.append("  neg_false_positive (hierarchy exists but suite expects disconnect):")
        for r in negfps[:40]:
            lines.append(f"    {r['check_id']}")
    text = "\n".join(lines) + "\n"
    (work / "pyslangwalk.report").write_text(text, encoding="utf-8")
    print(text, flush=True)
    return 1 if must_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
