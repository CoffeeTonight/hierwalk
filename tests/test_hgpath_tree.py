"""hgpath tree DB and path norm tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from hgpath.path_norm import common_prefix_segments, normalize_spec
from hgpath.tree_db import TreeDb, TreeNode, resolve_tree_db_path


def test_normalize_spec_splits_leaf_tail():
    n = normalize_spec("top.u_a.out", top="top")
    assert n.inst_key == "top.u_a"
    assert n.leaf_tail == "out"
    assert n.full_key == "top.u_a.out"


def test_common_prefix_segments():
    paths = ["top.cpu.u0.cache", "top.cpu.u1.cache", "top.dma.buf"]
    assert common_prefix_segments(paths) == ("top",)


def test_tree_db_lpm_prefix(tmp_path: Path):
    db = TreeDb(work_dir=tmp_path, path=resolve_tree_db_path(tmp_path))
    result = {
        "ok": True,
        "ambiguous": False,
        "error": "",
        "nodes": [
            {
                "segment": "top",
                "role": "root",
                "module": "top",
                "file": "/a/top.v",
                "hit_file": "/a/top.v",
            },
            {
                "segment": "u_a",
                "role": "inst",
                "module": "top",
                "child_module": "child",
                "child_decl_file": "/a/top.v",
                "file": "/a/top.v",
            },
        ],
    }
    db.insert_result("top.u_a", result)
    ent, shared = db.longest_prefix("top.u_a.sig")
    assert shared == 2
    assert ent is not None
    assert ent.key == "top.u_a"


def test_tree_node_requires_file():
    with pytest.raises(ValueError, match="missing file"):
        TreeNode.from_resolve_node(path="top", node={"segment": "top", "role": "root", "module": "top"})