"""User-facing hierarchy → node → RTL file JSON (companion to human .report)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from hierwalk.connect.shared.request import ConnectivityCheck

from hgpath.tree_db import TreeEntry, TreeNode

HIERARCHY_JSON_NAME = "hgpath.hierarchy.json"
HIERARCHY_SCHEMA_VERSION = 1


def resolve_hierarchy_json_path(work_dir: str | Path) -> Path:
    return Path(work_dir).expanduser().resolve() / HIERARCHY_JSON_NAME


def _node_dict(node: TreeNode, *, hop: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "hop": hop,
        "path": node.path,
        "segment": node.segment,
        "role": node.role,
        "module": node.module,
        "file": node.file,
    }
    if node.kind:
        out["kind"] = node.kind
    if node.child_module:
        out["child_module"] = node.child_module
    return out


def _endpoint_dict(hierarchy: str, entry: TreeEntry) -> Dict[str, Any]:
    return {
        "hierarchy": hierarchy,
        "key": entry.key,
        "ok": entry.ok,
        "ambiguous": entry.ambiguous,
        "error": entry.error or "",
        "port_tail": entry.port_tail or "",
        "nodes": [_node_dict(n, hop=i) for i, n in enumerate(entry.nodes)],
        "scoped_files": list(entry.scoped_files),
    }


def build_hgpath_hierarchy_json(
    *,
    top: str,
    check_results: Sequence[Tuple[ConnectivityCheck, TreeEntry, TreeEntry]],
    simple_exist: bool = False,
    tool: str = "hgpath",
) -> Dict[str, Any]:
    """Build check-aligned hierarchy export: every hop + RTL file per JSON endpoint."""
    endpoints: List[Dict[str, Any]] = []
    seen: set[str] = set()
    checks_out: List[Dict[str, Any]] = []

    for chk, ea, eb in check_results:
        ha = str(chk.endpoint_a)
        hb = str(chk.endpoint_b)
        for hierarchy, entry in ((ha, ea), (hb, eb)):
            if hierarchy not in seen:
                seen.add(hierarchy)
                endpoints.append(_endpoint_dict(hierarchy, entry))
        checks_out.append(
            {
                "check_id": chk.check_id or "",
                "endpoint_a": {"hierarchy": ha, "key": ea.key},
                "endpoint_b": {"hierarchy": hb, "key": eb.key},
                "pair_ok": bool(ea.ok and eb.ok),
                "endpoint_a_ok": ea.ok,
                "endpoint_b_ok": eb.ok,
            }
        )

    return {
        "schema_version": HIERARCHY_SCHEMA_VERSION,
        "tool": tool,
        "top": top,
        "simple_exist": bool(simple_exist),
        "endpoint_count": len(endpoints),
        "check_count": len(checks_out),
        "endpoints": endpoints,
        "checks": checks_out,
    }


def write_hgpath_hierarchy_json(
    path: str | Path,
    *,
    top: str,
    check_results: Sequence[Tuple[ConnectivityCheck, TreeEntry, TreeEntry]],
    simple_exist: bool = False,
    tool: str = "hgpath",
) -> Path:
    out = Path(path).expanduser().resolve()
    payload = build_hgpath_hierarchy_json(
        top=top,
        check_results=check_results,
        simple_exist=simple_exist,
        tool=tool,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return out