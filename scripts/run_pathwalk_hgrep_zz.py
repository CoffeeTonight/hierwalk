#!/usr/bin/env python3
"""Path-walk connect with connect_phase=hgrep (grep_hie gate) on zigzag suite."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hierwalk.connect.shared.request import load_connect_request  # noqa: E402
from hierwalk.filelist import parse_filelist  # noqa: E402
from hierwalk.path_walk import run_path_walk_connect  # noqa: E402
from hierwalk.zigzag_torture_gen import SUITE_CONN_NEGATIVE_IDS  # noqa: E402


def main() -> int:
    zz = Path("/home/user/Desktop/hgrep_demo/.zz_verify").resolve()
    work = zz / ".db_pathwalk_hgrep"
    work.mkdir(parents=True, exist_ok=True)

    req = load_connect_request(zz / "zz_torture.connect.json")
    fl = parse_filelist(
        str(zz / "filelist.f"),
        index_cwd=str(zz),
        extra_defines=dict(req.defines) or None,
    )
    print(
        f"path-walk hgrep: checks={len(req.checks)} sources={len(fl.source_files)} "
        f"top={req.top} work={work}",
        flush=True,
    )

    t0 = time.perf_counter()
    batch, index, state = run_path_walk_connect(
        req,
        fl,
        top=req.top,
        extra_defines=dict(req.defines),
        no_cache=False,
        connect_phase="hgrep",
        connect_output_dir=work,
        connect_output_name="conn_hgrep.tsv",
        refresh_cache=False,
    )
    elapsed = time.perf_counter() - t0

    rows = []
    n_conn = 0
    n_hier_fail = 0
    n_neg_ok = 0
    n_neg_fp = 0
    n_must_ok = 0
    n_must_fail = 0

    for r in batch.results:
        cid = r.check_id or ""
        connected = bool(r.connected)
        if r.sub_results:
            connected = all(sr.connected for sr in r.sub_results)
        errors = list(r.errors or [])
        note = r.note or ""
        is_neg = cid in SUITE_CONN_NEGATIVE_IDS
        if connected:
            n_conn += 1
        else:
            if any("hierarchy" in e.lower() for e in errors) or "hierarchy" in note.lower():
                n_hier_fail += 1

        if is_neg:
            if not connected:
                n_neg_ok += 1
                verdict = "neg_ok"
            else:
                n_neg_fp += 1
                verdict = "neg_false_positive"
        else:
            if connected:
                n_must_ok += 1
                verdict = "must_ok"
            else:
                n_must_fail += 1
                verdict = "must_fail"

        rows.append(
            {
                "check_id": cid,
                "connected": connected,
                "errors": errors,
                "note": note,
                "verdict": verdict,
                "endpoint_a": r.endpoint_a.spec if r.endpoint_a else "",
                "endpoint_b": r.endpoint_b.spec if r.endpoint_b else "",
            }
        )

    summary = {
        "phase": "hgrep",
        "method": "path-walk connect_phase=hgrep (grep_hie gate)",
        "total": len(batch.results),
        "connected": n_conn,
        "must_ok": n_must_ok,
        "must_fail": n_must_fail,
        "neg_ok": n_neg_ok,
        "neg_false_positive": n_neg_fp,
        "hierarchy_fail_rows": n_hier_fail,
        "modules": len(getattr(index, "modules", {}) or {}),
        "elapsed_sec": round(elapsed, 2),
        "work_dir": str(work),
    }

    report = {"summary": summary, "rows": rows}
    report_path = work / "pathwalk_hgrep_summary.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")

    # Human summary
    lines = [
        "path-walk + grep hie (connect_phase=hgrep)",
        f"  total checks:     {summary['total']}",
        f"  connected:        {summary['connected']}",
        f"  must_ok:          {summary['must_ok']}",
        f"  must_fail:        {summary['must_fail']}",
        f"  neg_ok:           {summary['neg_ok']}",
        f"  neg_false_pos:    {summary['neg_false_positive']}",
        f"  modules:          {summary['modules']}",
        f"  elapsed_sec:      {summary['elapsed_sec']}",
        f"  work_dir:         {work}",
        f"  summary_json:     {report_path}",
    ]
    must_fails = [r for r in rows if r["verdict"] == "must_fail"]
    if must_fails:
        lines.append("  must_fail ids:")
        for r in must_fails[:30]:
            lines.append(f"    {r['check_id']}: {r['errors'] or r['note']}")
    text = "\n".join(lines) + "\n"
    (work / "pathwalk_hgrep.report").write_text(text, encoding="utf-8")
    print(text, flush=True)
    return 1 if n_must_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
