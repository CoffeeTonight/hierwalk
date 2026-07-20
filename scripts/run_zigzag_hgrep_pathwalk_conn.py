#!/usr/bin/env python3
"""Zigzag conn via hgrep gate + path-walk text-COI (connect_phase=text)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hierwalk.connect.shared.request import (  # noqa: E402
    ConnectivityRequest,
    load_connect_request,
)
from hierwalk.filelist import parse_filelist  # noqa: E402
from hierwalk.path_walk import run_path_walk_connect  # noqa: E402
from hierwalk.zigzag_torture_gen import SUITE_CONN_NEGATIVE_IDS  # noqa: E402

# Expand / list checks: path-walk uses sub_results when expanded.
ROUND18_EXPAND_IDS = frozenset(
    {
        "zz_fanin_merge",
        "zz_fanin_merge_decoy",
        "zz_port_expr_xor",
        "zz_expr_mapped",
        "zz_port_concat",
        "zz_port_expr_or",
        "zz_fanin_merge4",
        "zz_loop_range",
        "zz_loop_list",
        "zz_loop_csv",
        "zz_literal_concat",
        "zz_list_endpoints",
        "zz_list_expand",
        "zz_list_display",
        "zz_hier_array",
        "zz_array_zip",
        "zz_wire_list_display",
        "zz_fanout_mid",
    }
)


def _connected_ok(result) -> Tuple[bool, str]:
    cid = result.check_id or ""
    if result.sub_results:
        ok = all(sr.connected for sr in result.sub_results)
        return ok, "expand-all" if ok else "expand-partial-fail"
    if result.connected:
        return True, result.note or "connected"
    errors = list(result.errors or [])
    return False, f"errors={errors} note={result.note or ''}"


def main() -> int:
    zz = Path("/home/user/Desktop/hgrep_demo/.zz_verify").resolve()
    work = zz / ".db_zz_hgrep_pw"
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

    print(
        f"hgrep+path-walk conn: checks={len(req.checks)} sources={len(fl.source_files)} "
        f"top={req.top} phase=text work={work}",
        flush=True,
    )

    t0 = time.perf_counter()
    batch, index, state = run_path_walk_connect(
        req,
        fl,
        top=req.top,
        extra_defines=dict(req.defines),
        no_cache=False,
        connect_phase="text",
        connect_output_dir=work,
        connect_output_name="conn_hgrep_pw.tsv",
        refresh_cache=False,
    )
    elapsed = time.perf_counter() - t0

    rows: List[Dict] = []
    must_ok = must_fail = neg_ok = neg_fp = 0
    expand_ok = expand_fail = 0

    for r in batch.results:
        cid = r.check_id or ""
        ok, detail = _connected_ok(r)
        is_neg = cid in SUITE_CONN_NEGATIVE_IDS
        is_expand = cid in ROUND18_EXPAND_IDS or bool(r.sub_results)

        if is_neg:
            if ok:
                neg_fp += 1
                verdict = "neg_false_positive"
            else:
                neg_ok += 1
                verdict = "neg_ok"
        elif is_expand:
            if ok:
                expand_ok += 1
                verdict = "expand_ok"
            else:
                expand_fail += 1
                verdict = "expand_fail"
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
                "detail": detail,
                "mode": r.mode if hasattr(r, "mode") else "",
                "endpoint_a": r.endpoint_a.spec if r.endpoint_a else "",
                "endpoint_b": r.endpoint_b.spec if r.endpoint_b else "",
            }
        )

    summary = {
        "method": "hgrep_gate + path_walk text_coi",
        "connect_phase": "text",
        "total": len(batch.results),
        "connected": sum(1 for r in rows if r["connected"]),
        "must_ok": must_ok,
        "must_fail": must_fail,
        "expand_ok": expand_ok,
        "expand_fail": expand_fail,
        "neg_ok": neg_ok,
        "neg_false_positive": neg_fp,
        "elapsed_sec": round(elapsed, 2),
        "modules_index": len(getattr(index, "modules", {}) or {}),
        "work_dir": str(work),
        "handoff": str(work / "hgrep_pathwalk_handoff.json"),
        "gate_report": str(work / "conn_hgrep_pw_hgrep_gate.report")
        if (work / "conn_hgrep_pw_hgrep_gate.report").is_file()
        else str(work / "conn.hgrep_gate.report"),
    }

    out_json = work / "hgrep_pathwalk_conn_summary.json"
    out_json.write_text(
        json.dumps({"summary": summary, "rows": rows}, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    lines = [
        "hgrep + path-walk text-COI (zigzag conn)",
        f"  total:              {summary['total']}",
        f"  connected:          {summary['connected']}",
        f"  must_ok:            {must_ok}",
        f"  must_fail:          {must_fail}",
        f"  expand_ok:          {expand_ok}",
        f"  expand_fail:        {expand_fail}",
        f"  neg_ok:             {neg_ok}",
        f"  neg_false_positive: {neg_fp}",
        f"  elapsed_sec:        {summary['elapsed_sec']}",
        f"  work_dir:           {work}",
        f"  summary_json:       {out_json}",
    ]
    fails = [r for r in rows if r["verdict"] in ("must_fail", "expand_fail")]
    if fails:
        lines.append("  failures:")
        for r in fails[:40]:
            lines.append(f"    {r['check_id']}: {r['detail'][:120]}")
    if neg_fp:
        lines.append("  neg false-positives:")
        for r in rows:
            if r["verdict"] == "neg_false_positive":
                lines.append(f"    {r['check_id']}")
    text = "\n".join(lines) + "\n"
    (work / "hgrep_pathwalk_conn.report").write_text(text, encoding="utf-8")
    print(text, flush=True)

    # Exit 0 only if no must/expand fails (neg FP still noted)
    return 1 if (must_fail or expand_fail) else 0


if __name__ == "__main__":
    raise SystemExit(main())
