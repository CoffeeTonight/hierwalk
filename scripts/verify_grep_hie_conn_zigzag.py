#!/usr/bin/env python3
"""Run zigzag conn suite: path-walk vs grep-hie (hgpath + hgconn bloom)."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hierwalk.connect.shared.request import (  # noqa: E402
    ConnectivityCheck,
    ConnectivityRequest,
    load_connect_request,
)
from hierwalk.filelist import parse_filelist  # noqa: E402
from hierwalk.path_walk import run_path_walk_connect  # noqa: E402
from hierwalk.zigzag_torture_gen import SUITE_CONN_NEGATIVE_IDS  # noqa: E402

from hg_core.hierarchy_json import write_hgpath_hierarchy_json  # noqa: E402
from hgpath.batch import run_batch  # noqa: E402
from hgpath.flat_db import load_or_build_flat_db  # noqa: E402
from hgpath.tree_db import TreeDb, resolve_tree_db_path  # noqa: E402
from hgconn.walk import run_bloom_batch  # noqa: E402

# Mirrors tests/test_zigzag_torture.py policy subsets used in connect-all.
ROUND18_NEGATIVE_CHECK_IDS = frozenset(
    {
        "zz_missing_hierarchy",
        "zz_fanin_merge_decoy",
        "zz_ifdef_inactive",
        "zz_multi_g3_empty",
    }
)
ROUND18_EXPAND_CHECK_IDS = frozenset(
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
    }
)


@dataclass
class Expect:
    want_connected: Optional[bool]
    reason: str


def _is_scalar_check(chk: ConnectivityCheck) -> bool:
    if chk.expand is not None:
        return False
    a, b = chk.endpoint_a, chk.endpoint_b
    if a.startswith("[") or b.startswith("["):
        return False
    return True


def expected_for_check(chk: ConnectivityCheck) -> Expect:
    cid = chk.check_id or ""
    if not _is_scalar_check(chk):
        return Expect(None, "expand/list — path-walk only")
    if cid.startswith("zz_vuln_"):
        return Expect(None, "vuln annex — skip parity")
    if cid in ROUND18_NEGATIVE_CHECK_IDS or cid in SUITE_CONN_NEGATIVE_IDS:
        return Expect(False, "negative suite")
    if cid in ROUND18_EXPAND_CHECK_IDS:
        return Expect(None, "expand sub-checks — path-walk only")
    return Expect(True, "must connect")


def _grep_hie_batch(
    checks: Sequence[ConnectivityCheck],
    *,
    work_dir: Path,
    filelist: str,
    index_cwd: str,
    top: str,
    defines: Dict[str, str],
) -> Tuple[Dict[str, Tuple], List]:
    fl = parse_filelist(filelist, index_cwd=index_cwd, extra_defines=defines or None)
    sources = [str(p) for p in fl.source_files]
    flat_db, session = load_or_build_flat_db(
        sources,
        top=top,
        work_dir=work_dir,
        refresh=True,
        filelist=filelist,
        index_cwd=index_cwd,
        on_log=lambda m: print(f"[hgpath] {m}", flush=True),
    )
    if defines:
        session.defines = dict(defines)
    tree = TreeDb(work_dir=work_dir, path=resolve_tree_db_path(work_dir))
    batch = run_batch(
        checks,
        top=top,
        session=session,
        tree=tree,
        on_log=lambda m: print(f"[hgpath] {m}", flush=True),
    )
    tree.save_if_changed()
    write_hgpath_hierarchy_json(
        work_dir / "hgpath.hierarchy.json",
        top=top,
        check_results=batch.check_results,
    )
    conn_results = run_bloom_batch(
        batch.check_results,
        on_log=lambda m: print(f"[hgconn] {m}", flush=True),
    )
    by_id = {r.check_id: r for r in conn_results}
    out: Dict[str, Tuple] = {}
    for chk, ea, eb in batch.check_results:
        cid = chk.check_id or ""
        cr = by_id.get(cid)
        hg_ok = bool(ea.ok and eb.ok and cr and cr.connected)
        detail = ""
        if cr:
            detail = f"mode={cr.mode} {cr.detail}"
        elif not (ea.ok and eb.ok):
            detail = f"hierarchy fail a={ea.error} b={eb.error}"
        out[cid] = (hg_ok, detail, ea, eb, cr)
    return out, batch.check_results


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--zz-dir",
        type=Path,
        default=Path("/home/user/Desktop/hgrep_demo/.zz_verify"),
        help="zigzag torture artifact dir",
    )
    ap.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="hgpath/hgconn work dir (default: <zz-dir>/.db_hgconn_zz)",
    )
    ap.add_argument(
        "--skip-path-walk",
        action="store_true",
        help="only run grep-hie (faster)",
    )
    args = ap.parse_args()

    zz_dir = args.zz_dir.expanduser().resolve()
    work_dir = (args.work_dir or zz_dir / ".db_hgconn_zz").expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    conn_path = zz_dir / "zz_torture.connect.json"
    req = load_connect_request(conn_path)
    top = req.top
    defines = dict(req.defines)
    filelist = str(zz_dir / "filelist.f")
    index_cwd = str(zz_dir)

    scalar_checks = [c for c in req.checks if _is_scalar_check(c)]
    expand_checks = [c for c in req.checks if not _is_scalar_check(c)]

    print(f"zigzag conn suite: total={len(req.checks)} scalar={len(scalar_checks)} expand={len(expand_checks)}")
    print(f"top={top} defines={defines}")
    print(f"work_dir={work_dir}")

    fl = parse_filelist(filelist, index_cwd=index_cwd, extra_defines=defines or None)

    pw_results: Dict[str, Tuple[bool, str]] = {}
    if not args.skip_path_walk:
        t0 = time.perf_counter()
        batch_req = ConnectivityRequest(
            checks=req.checks,
            top=top,
            defines=defines,
            include_ff=True,
        )
        pw_batch, _index, _state = run_path_walk_connect(
            batch_req,
            fl,
            top=top,
            no_cache=True,
        )
        by_cid = {r.check_id: r for r in pw_batch.results}
        for chk in req.checks:
            cid = chk.check_id or ""
            result = by_cid.get(cid)
            if result is None:
                pw_results[cid] = (False, "missing batch result")
                continue
            if cid in ROUND18_EXPAND_CHECK_IDS:
                ok = bool(result.sub_results) and all(
                    sr.connected for sr in result.sub_results
                )
                pw_results[cid] = (
                    ok,
                    "expand-all-connected" if ok else "expand-sub-fail",
                )
            elif cid in ROUND18_NEGATIVE_CHECK_IDS or cid in SUITE_CONN_NEGATIVE_IDS:
                if cid == "zz_missing_hierarchy":
                    if result.connected:
                        pw_results[cid] = (False, "expected disconnected")
                    elif not any("hierarchy" in e.lower() for e in result.errors):
                        pw_results[cid] = (False, "expected hierarchy error")
                    else:
                        pw_results[cid] = (True, "disconnected hierarchy-miss")
                elif result.connected:
                    pw_results[cid] = (False, "expected disconnected")
                else:
                    pw_results[cid] = (True, "disconnected ok")
            elif result.connected:
                pw_results[cid] = (True, result.note or "connected")
            else:
                pw_results[cid] = (
                    False,
                    f"errors={result.errors} note={result.note}",
                )
        print(f"[path-walk] batch {len(req.checks)} checks elapsed={time.perf_counter()-t0:.1f}s")

    t0 = time.perf_counter()
    hg_results, check_results = _grep_hie_batch(
        scalar_checks,
        work_dir=work_dir,
        filelist=filelist,
        index_cwd=index_cwd,
        top=top,
        defines=defines,
    )
    print(f"[grep-hie] elapsed={time.perf_counter()-t0:.1f}s")

    rows: List[Dict[str, object]] = []
    parity_fail: List[str] = []
    hg_fail: List[str] = []
    skipped = 0

    for chk in req.checks:
        cid = chk.check_id or ""
        exp = expected_for_check(chk)
        pw_ok, pw_detail = pw_results.get(cid, (None, "skipped"))
        hg = hg_results.get(cid)
        if exp.want_connected is None:
            skipped += 1
            rows.append(
                {
                    "check_id": cid,
                    "expect": exp.reason,
                    "path_walk": pw_ok,
                    "grep_hie": None if hg is None else hg[0],
                    "parity": "skip",
                    "detail": exp.reason,
                }
            )
            continue

        hg_ok = hg[0] if hg else False
        hg_detail = hg[1] if hg else "not scalar / not run"
        want = exp.want_connected
        pw_pass = pw_ok is True if pw_ok is not None else None
        hg_pass = hg_ok == want if hg else False

        if pw_pass is False:
            parity_fail.append(f"path-walk {cid}: want={want} got={pw_ok} ({pw_detail})")
        if not hg_pass:
            hg_fail.append(f"grep-hie {cid}: want={want} got={hg_ok} ({hg_detail})")

        parity = "ok"
        if pw_pass is not None and pw_pass != hg_pass:
            parity = "mismatch"
        elif pw_pass is False or not hg_pass:
            parity = "fail"

        rows.append(
            {
                "check_id": cid,
                "expect": "connected" if want else "disconnected",
                "path_walk": pw_ok,
                "grep_hie": hg_ok,
                "parity": parity,
                "path_walk_detail": pw_detail,
                "grep_hie_detail": hg_detail,
            }
        )

    summary = {
        "total_checks": len(req.checks),
        "scalar_checks": len(scalar_checks),
        "expand_skipped": len(expand_checks),
        "policy_skipped": skipped - len(expand_checks),
        "path_walk_fail": len(parity_fail),
        "grep_hie_fail": len(hg_fail),
        "parity_mismatch": sum(1 for r in rows if r.get("parity") == "mismatch"),
    }

    report_path = work_dir / "grep_hie_conn_parity.json"
    report_path.write_text(
        json.dumps({"summary": summary, "rows": rows}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print("\n=== SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"  report: {report_path}")

    if parity_fail:
        print("\npath-walk failures (baseline):")
        for line in parity_fail[:20]:
            print(f"  {line}")
        if len(parity_fail) > 20:
            print(f"  ... +{len(parity_fail)-20} more")

    if hg_fail:
        print("\ngrep-hie failures:")
        for line in hg_fail[:30]:
            print(f"  {line}")
        if len(hg_fail) > 30:
            print(f"  ... +{len(hg_fail)-30} more")

    mism = [r for r in rows if r.get("parity") == "mismatch"]
    if mism:
        print("\nparity mismatches (path-walk vs grep-hie expectation):")
        for r in mism[:20]:
            print(f"  {r['check_id']}: pw={r['path_walk']} hg={r['grep_hie']} expect={r['expect']}")

    return 1 if (parity_fail or hg_fail) else 0


if __name__ == "__main__":
    raise SystemExit(main())