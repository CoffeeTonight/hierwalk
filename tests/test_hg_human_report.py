"""Human-readable report table tests."""

from __future__ import annotations

from hierwalk.connect.shared.request import ConnectivityCheck

from hg_core.human_report import build_hgconn_human_report, build_hgpath_human_report, render_table
from hgconn.walk import ConnResult
from hgpath.tree_db import TreeEntry, TreeNode


def _entry(key: str, *, ok: bool, error: str = "", port: str = "") -> TreeEntry:
    nodes = (
        TreeNode(path="top", segment="top", role="root", module="top", file="/a/top.v"),
        TreeNode(
            path="top.u_a",
            segment="u_a",
            role="inst",
            module="top",
            file="/a/top.v",
            child_module="child",
        ),
    )
    if port:
        nodes = (
            nodes[0],
            TreeNode(
                path="top.u_a",
                segment="u_a",
                role="inst",
                module="top",
                file="/a/top.v",
                kind="port",
            ),
        )
    return TreeEntry(
        key=key,
        ok=ok,
        ambiguous=False,
        error=error,
        port_tail=port,
        nodes=nodes if ok else (nodes[0],),
        scoped_files=("/a/top.v",),
        resolve_result={},
    )


def test_render_table_aligns_columns():
    lines = render_table(
        ("id", "name"),
        [("hg1", "top.u_a"), ("hg22", "top.u_b")],
        aligns=("left", "left"),
    )
    assert len(lines) == 4
    assert "id" in lines[0]
    assert "hg1" in lines[2]


def test_hgpath_human_report_sections():
    ea = _entry("top.u_a", ok=True, port="out")
    eb = _entry("top.u_b", ok=False, error="instance top.u_b not found")
    chk = ConnectivityCheck("top.u_a.out", "top.u_b.out", check_id="c1")
    lines = build_hgpath_human_report(
        top="top",
        entries={"top.u_a": ea, "top.u_b": eb},
        check_results=[(chk, ea, eb)],
    )
    text = "\n".join(lines)
    assert "1. SUMMARY" in text
    assert "2. HIERARCHY ENDPOINTS" in text
    assert "4. HIERARCHY" in text
    assert "top.u_a.out" in text
    assert "| a " in text or "| a  " in text
    assert "FAIL" in text
    assert "3. HIERARCHY GREP TEXT-CONN" not in text


def test_hgconn_human_report_has_text_conn_table():
    ea = _entry("top.u_a", ok=True, port="out")
    eb = _entry("top.u_b", ok=True, port="in")
    chk = ConnectivityCheck("top.u_a.out", "top.u_b.in", check_id="c1")
    conn = ConnResult("c1", True, "same-net", "", 1.5)
    lines = build_hgconn_human_report(
        top="top",
        entries={"top.u_a": ea, "top.u_b": eb},
        check_results=[(chk, ea, eb)],
        conn_results=[conn],
    )
    text = "\n".join(lines)
    assert "3. HIERARCHY GREP TEXT-CONN" in text
    assert "same-net" in text
    assert "YES" in text