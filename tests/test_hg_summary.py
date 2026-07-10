"""hg_core report summary tests."""

from __future__ import annotations

from hierwalk.connect.shared.request import ConnectivityCheck

from hg_core.summary import (
    classify_endpoint_failure,
    summarize_connectivity,
    summarize_hierarchy_checks,
    summarize_hierarchy_endpoints,
)
from hgconn.walk import ConnResult
from hgpath.tree_db import TreeEntry, TreeNode


def _entry(key: str, *, ok: bool, error: str = "", ambiguous: bool = False) -> TreeEntry:
    nodes = (
        TreeNode(path="top", segment="top", role="root", module="top", file="/a/top.v"),
        TreeNode(path="top.u", segment="u", role="inst", module="top", file="/a/top.v"),
    )
    return TreeEntry(
        key=key,
        ok=ok,
        ambiguous=ambiguous,
        error=error,
        port_tail="",
        nodes=nodes if ok else (nodes[:1]),
        scoped_files=("/a/top.v",),
        resolve_result={},
    )


def test_summarize_endpoints_pass_fail_pct():
    entries = {
        "top.u_a": _entry("top.u_a", ok=True),
        "top.u_b": _entry("top.u_b", ok=False, error="instance top.u_b not found"),
    }
    lines = summarize_hierarchy_endpoints(entries)
    text = "\n".join(lines)
    assert "paths: 2  pass: 1  fail: 1  success: 50.0%" in text
    assert "inst-not-found: 1" in text
    assert "top.u_b:" in text


def test_summarize_check_pairs():
    ea = _entry("top.u_a", ok=True)
    eb = _entry("top.u_b", ok=False, error="leaf missing")
    chk = ConnectivityCheck("top.u_a.out", "top.u_b.out", check_id="hg1")
    lines = summarize_hierarchy_checks([(chk, ea, eb)])
    text = "\n".join(lines)
    assert "checks: 1  pass: 0  fail: 1" in text
    assert "endpoint-b-fail: 1" in text
    assert "hg1:" in text


def test_summarize_connectivity_modes():
    results = [
        ConnResult("hg1", True, "same-net", "", 1.0),
        ConnResult("hg2", False, "miss", "no assign/word hit", 2.0),
    ]
    lines = summarize_connectivity(results)
    text = "\n".join(lines)
    assert "connected: 1" in text
    assert "success: 50.0%" in text
    assert "same-net: 1" in text
    assert "bloom-miss: 1" in text


def test_classify_ambiguous():
    ent = _entry("x", ok=False, ambiguous=True)
    assert classify_endpoint_failure(ent) == "ambiguous"