"""User-facing hgpath.hierarchy.json export."""

from __future__ import annotations

import json
from pathlib import Path

from hierwalk.connect.shared.request import ConnectivityCheck

from hg_core.hierarchy_json import build_hgpath_hierarchy_json, write_hgpath_hierarchy_json
from hgpath.tree_db import TreeEntry, TreeNode


def _entry(key: str, *, ok: bool = True) -> TreeEntry:
    nodes = (
        TreeNode(
            path="top",
            segment="top",
            role="root",
            module="top",
            file="/rtl/top.v",
        ),
        TreeNode(
            path=key,
            segment=key.split(".")[-1],
            role="leaf",
            module="top",
            file="/rtl/top.v",
            kind="inst",
            child_module="LeafA",
        ),
    )
    return TreeEntry(
        key=key,
        ok=ok,
        ambiguous=False,
        error="",
        port_tail="",
        nodes=nodes,
        scoped_files=("/rtl/top.v",),
        resolve_result={},
    )


def test_build_hgpath_hierarchy_json_structure():
    chk = ConnectivityCheck("/top/u_A", "/top/u_B", check_id="c1")
    payload = build_hgpath_hierarchy_json(
        top="top",
        check_results=[(chk, _entry("top.u_A"), _entry("top.u_B"))],
        simple_exist=True,
    )
    assert payload["schema_version"] == 1
    assert payload["simple_exist"] is True
    assert payload["endpoint_count"] == 2
    ep_a = payload["endpoints"][0]
    assert ep_a["hierarchy"] == "/top/u_A"
    assert len(ep_a["nodes"]) == 2
    assert ep_a["nodes"][0]["file"] == "/rtl/top.v"
    assert ep_a["nodes"][1]["child_module"] == "LeafA"
    assert payload["checks"][0]["pair_ok"] is True


def test_write_hgpath_hierarchy_json(tmp_path: Path):
    chk = ConnectivityCheck("top.u_A.out", "top.u_A.out", check_id="c1")
    path = write_hgpath_hierarchy_json(
        tmp_path / "hgpath.hierarchy.json",
        top="top",
        check_results=[(chk, _entry("top.u_A"), _entry("top.u_A"))],
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["endpoints"][0]["scoped_files"] == ["/rtl/top.v"]