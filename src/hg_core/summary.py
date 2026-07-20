"""Hierarchy / connectivity check summaries for hgpath & hgconn reports."""

from __future__ import annotations

from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from hierwalk.connect.shared.request import ConnectivityCheck

from hgconn.walk import ConnResult
from hgpath.tree_db import TreeEntry, TreeNode


def _pct(pass_n: int, total: int) -> str:
    if total <= 0:
        return "n/a"
    return f"{100.0 * pass_n / total:.1f}%"


def classify_endpoint_failure(entry: TreeEntry) -> str:
    if entry.ok:
        return "pass"
    if entry.ambiguous:
        return "ambiguous"
    err = (entry.error or "").strip().lower()
    if "instance" in err and "not found" in err:
        return "inst-not-found"
    if "not in grep index" in err:
        return "top-not-indexed"
    if "empty hierarchy" in err:
        return "empty-path"
    if "no matching declaration" in err:
        return "leaf-not-found"
    if err:
        return "resolve-error"
    return "unknown"


def classify_check_pair(entry_a: TreeEntry, entry_b: TreeEntry) -> str:
    if entry_a.ok and entry_b.ok:
        return "pass"
    if not entry_a.ok and not entry_b.ok:
        return "both-fail"
    if not entry_a.ok:
        return "endpoint-a-fail"
    return "endpoint-b-fail"


def _counter_lines(label: str, counts: Counter[str], *, skip_pass: bool = True) -> List[str]:
    items = sorted(
        ((k, v) for k, v in counts.items() if not (skip_pass and k == "pass")),
        key=lambda kv: (-kv[1], kv[0]),
    )
    if not items:
        return []
    lines = [f"  {label}:"]
    for key, n in items:
        lines.append(f"    {key}: {n}")
    return lines


def _hop_stats(entries: Iterable[TreeEntry]) -> Tuple[int, int, float]:
    hops = [len(e.nodes) for e in entries if e.nodes]
    if not hops:
        return 0, 0, 0.0
    return min(hops), max(hops), sum(hops) / len(hops)


def summarize_hierarchy_endpoints(
    entries: Dict[str, TreeEntry],
    *,
    max_fail_detail: int = 20,
) -> List[str]:
    """Unique a/b endpoint hierarchy paths resolved in tree batch."""
    if not entries:
        return []

    vals = list(entries.values())
    pass_n = sum(1 for e in vals if e.ok)
    total = len(vals)
    fail_n = total - pass_n
    causes = Counter(classify_endpoint_failure(e) for e in vals)
    min_h, max_h, avg_h = _hop_stats(vals)

    lines = [
        "--- hierarchy endpoints (unique a/b paths) ---",
        f"paths: {total}  pass: {pass_n}  fail: {fail_n}  success: {_pct(pass_n, total)}",
        f"inst_hops: min={min_h} max={max_h} avg={avg_h:.1f}",
    ]
    lines.extend(_counter_lines("fail_by_cause", causes))

    fails = [(k, e) for k, e in sorted(entries.items()) if not e.ok]
    if fails:
        lines.append("failed_paths:")
        for key, ent in fails[:max_fail_detail]:
            cause = classify_endpoint_failure(ent)
            err = ent.error or "ambiguous" if ent.ambiguous else "unknown"
            lines.append(f"  {key}: [{cause}] {err}")
        if len(fails) > max_fail_detail:
            lines.append(f"  ... and {len(fails) - max_fail_detail} more")
    return lines


def summarize_hierarchy_checks(
    check_results: Sequence[Tuple[ConnectivityCheck, TreeEntry, TreeEntry]],
    *,
    max_fail_detail: int = 20,
) -> List[str]:
    """a/b check pairs — both endpoints must resolve."""
    if not check_results:
        return []

    outcomes = Counter()
    for _chk, ea, eb in check_results:
        outcomes[classify_check_pair(ea, eb)] += 1

    pass_n = outcomes.get("pass", 0)
    total = len(check_results)
    fail_n = total - pass_n

    lines = [
        "--- hierarchy check pairs (a,b) ---",
        f"checks: {total}  pass: {pass_n}  fail: {fail_n}  success: {_pct(pass_n, total)}",
    ]
    lines.extend(_counter_lines("fail_by_cause", outcomes))

    fails = [
        (chk, ea, eb)
        for chk, ea, eb in check_results
        if not (ea.ok and eb.ok)
    ]
    if fails:
        lines.append("failed_checks:")
        for chk, ea, eb in fails[:max_fail_detail]:
            cid = chk.check_id or "-"
            a_stat = "ok" if ea.ok else f"FAIL({classify_endpoint_failure(ea)})"
            b_stat = "ok" if eb.ok else f"FAIL({classify_endpoint_failure(eb)})"
            lines.append(
                f"  {cid}: a={chk.endpoint_a} [{a_stat}]  "
                f"b={chk.endpoint_b} [{b_stat}]"
            )
        if len(fails) > max_fail_detail:
            lines.append(f"  ... and {len(fails) - max_fail_detail} more")
    return lines


def summarize_connectivity(
    conn_results: Sequence["ConnResult"],
    *,
    max_fail_detail: int = 20,
) -> List[str]:
    if not conn_results:
        return []

    connected_n = sum(1 for r in conn_results if r.connected)
    total = len(conn_results)
    disconnected_n = total - connected_n
    fail_causes = Counter()
    pass_modes = Counter()
    for r in conn_results:
        if r.connected:
            pass_modes[r.mode] += 1
        elif r.mode == "hierarchy-miss":
            fail_causes["hierarchy-miss"] += 1
        elif r.mode == "miss":
            fail_causes["bloom-miss"] += 1
        else:
            fail_causes[r.mode] += 1

    lines = [
        "--- connectivity (bloom) ---",
        f"checks: {total}  connected: {connected_n}  "
        f"disconnected: {disconnected_n}  success: {_pct(connected_n, total)}",
    ]
    if pass_modes:
        lines.extend(_counter_lines("pass_by_mode", pass_modes, skip_pass=False))
    lines.extend(_counter_lines("fail_by_cause", fail_causes, skip_pass=False))

    fails = [r for r in conn_results if not r.connected]
    if fails:
        lines.append("disconnected_checks:")
        for r in fails[:max_fail_detail]:
            lines.append(
                f"  {r.check_id or '-'}: mode={r.mode} detail={r.detail}"
            )
        if len(fails) > max_fail_detail:
            lines.append(f"  ... and {len(fails) - max_fail_detail} more")
    return lines


def append_hgpath_summary(
    report,
    *,
    top: str = "",
    entries: Dict[str, TreeEntry],
    check_results: Sequence[Tuple[ConnectivityCheck, TreeEntry, TreeEntry]],
    db_info: Optional[Dict[str, object]] = None,
) -> None:
    from hg_core.human_report import build_hgpath_human_report

    for line in build_hgpath_human_report(
        top=top,
        entries=entries,
        check_results=check_results,
        db_info=db_info,
    ):
        report.add(line)


def append_hgconn_summary(
    report,
    *,
    top: str = "",
    entries: Dict[str, TreeEntry],
    check_results: Sequence[Tuple[ConnectivityCheck, TreeEntry, TreeEntry]],
    conn_results: Sequence["ConnResult"],
) -> None:
    from hg_core.human_report import build_hgconn_human_report

    for line in build_hgconn_human_report(
        top=top,
        entries=entries,
        check_results=check_results,
        conn_results=conn_results,
    ):
        report.add(line)