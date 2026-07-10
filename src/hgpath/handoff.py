"""Handoff: tree entries → FlatRow + scoped_files for hgconn."""

from __future__ import annotations

from typing import List, Sequence, Tuple

from hierwalk.index import DesignIndex
from hierwalk.models import FlatRow

from hgpath.tree_db import TreeEntry, TreeNode


def flat_rows_from_tree_entry(
    entry: TreeEntry,
    *,
    index: DesignIndex | None = None,
) -> Tuple[FlatRow, ...]:
    idx = index or DesignIndex({})
    rows: List[FlatRow] = []
    for node in entry.nodes:
        if node.role not in ("root", "inst", "leaf"):
            continue
        if node.role == "leaf" and node.kind not in (None, "inst"):
            continue
        parent = ".".join(node.path.split(".")[:-1]) or None
        depth = node.path.count(".")
        mod = node.child_module or node.module
        rows.append(
            FlatRow(
                full_path=node.path,
                inst_leaf=node.segment,
                module=mod,
                depth=depth,
                parent_path=parent,
                file=node.file,
                via_filelist=idx.filelist_for(node.file),
                filelist_chain=idx.filelist_chain_for(node.file),
                refine_status="hgpath",
            )
        )
    return tuple(rows)


def scoped_files_from_entry(entry: TreeEntry) -> Tuple[str, ...]:
    return entry.scoped_files