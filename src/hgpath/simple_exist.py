"""Optional slash-path existence: ``/top/u_a/b`` → ``\\bsegment\\b`` in parent RTL.

Preprocessing is **comment strip only** — no ``slim_body``, ifdef filter, or macro expand.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from hierwalk.hierarchy_grep import HierarchyGrepSession, _inst_leaf_word_in_body
from hierwalk.inst_scan import inst_base_name


def is_simple_exist_spec(spec: str) -> bool:
    """True when *spec* uses optional slash notation (``/top/inst/...``)."""
    return str(spec or "").strip().startswith("/")


def slash_spec_to_dots(spec: str) -> str:
    """``/top/u_a/out`` → ``top.u_a.out`` (for tree keys / reports)."""
    raw = str(spec or "").strip()
    if not raw.startswith("/"):
        return raw
    parts = [inst_base_name(p) for p in raw.split("/") if p]
    return ".".join(parts)


def _module_body(
    session: HierarchyGrepSession,
    module: str,
) -> Tuple[str, str]:
    files = session.module_index.get(module) or []
    if not files:
        return "", ""
    fpath = str(files[0])
    cache_key = (module, fpath)
    cached = session._module_body_cache.get(cache_key)
    if cached is not None:
        return cached, fpath
    try:
        text = Path(fpath).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return "", fpath
    session._module_body_cache[cache_key] = text
    return text, fpath


def _child_module_comment_only(parent_body: str, inst_seg: str) -> Optional[str]:
    """
    Resolve child cell type after comment strip only.

    Line walk: skip `` `directive`` lines; allow pending cell on prior line and
    ``#(...)`` params between cell and inst (no slim_body / ifdef filter).
    """
    import re

    from hierwalk.inst_scan import _KEYWORDS, inst_base_name, normalize_cell_module
    from hierwalk.preprocess import strip_comments_for_instance_scan

    work = strip_comments_for_instance_scan(parent_body)
    base = inst_base_name(inst_seg)
    if not work or not base:
        return None

    pending: Optional[str] = None
    for line in work.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("`"):
            continue
        if stripped.startswith("#") or re.match(r"^\s*#\s*\(", stripped):
            continue

        m_inst = re.search(
            rf"(?<!\w){re.escape(base)}(?!\w)\s*(?:\[[^\]]*\])?\s*(?:\(|\;)",
            stripped,
        )
        if m_inst:
            if pending:
                hit = normalize_cell_module(pending)
                if hit and hit.lower() not in _KEYWORDS:
                    return hit
            m_same = re.match(
                rf"^\s*(\w+)\s*(?:#\s*\([^)]*\))?\s*{re.escape(base)}\b",
                stripped,
            )
            if m_same:
                hit = normalize_cell_module(m_same.group(1))
                if hit and hit.lower() not in _KEYWORDS:
                    return hit
            return None

        m_cell = re.match(r"^\s*(\w+)\s*(?:#\s*\([^)]*\))?\s*$", stripped)
        if m_cell:
            cand = m_cell.group(1)
            if cand.lower() not in _KEYWORDS:
                pending = cand
            continue
        if re.match(r"^\s*(\w+)\s+#\s*\(", stripped):
            m_cp = re.match(r"^\s*(\w+)\s+#\s*\(", stripped)
            if m_cp and m_cp.group(1).lower() not in _KEYWORDS:
                pending = m_cp.group(1)
            continue
        pending = None
    return None


def _child_module_for_inst(
    session: HierarchyGrepSession,
    parent_body: str,
    inst_seg: str,
) -> Optional[str]:
    child = _child_module_comment_only(parent_body, inst_seg)
    if not child:
        return None
    from hierwalk.hierarchy_grep import _lookup_module_index

    return _lookup_module_index(session.module_index, child)


def resolve_simple_exist(
    session: HierarchyGrepSession,
    spec: str,
    *,
    top: str,
) -> Dict[str, Any]:
    """
    Minimal existence walk: each ``/seg`` must appear as ``\\bseg\\b`` in parent RTL.

    Preprocess: comment strip only. No ifdef branching, no multi-file fan-out.
    """
    raw = str(spec or "").strip()
    dots = slash_spec_to_dots(raw)
    top_name = inst_base_name(top.strip())
    parts = [inst_base_name(p) for p in dots.split(".") if p]

    if not parts:
        return _result(
            ok=False,
            hierarchy=dots,
            top=top_name,
            error="empty slash path",
            nodes=[],
        )

    if parts[0] != top_name:
        if top_name in session.module_index and len(parts) == 1:
            parts = [top_name]
        elif top_name in session.module_index:
            parts = [top_name, *parts] if parts[0] != top_name else parts
        else:
            parts = [top_name, *parts]

    if top_name not in session.module_index:
        return _result(
            ok=False,
            hierarchy=".".join(parts),
            top=top_name,
            error=f"top module {top_name!r} not in grep index",
            nodes=[],
        )

    nodes: List[Dict[str, Any]] = []
    top_file = str(session.module_index[top_name][0])
    nodes.append(
        {
            "segment": top_name,
            "role": "root",
            "module": top_name,
            "file": top_file,
            "hit_file": top_file,
            "found": True,
        }
    )

    current_mod = top_name
    current_file = top_file

    for i, seg in enumerate(parts[1:], start=1):
        body, fpath = _module_body(session, current_mod)
        if not body:
            return _result(
                ok=False,
                hierarchy=".".join(parts[: i + 1]),
                top=top_name,
                error=f"cannot read RTL for module {current_mod!r}",
                nodes=nodes,
                fail_segment=seg,
            )
        if not _inst_leaf_word_in_body(body, seg):
            return _result(
                ok=False,
                hierarchy=".".join(parts[: i + 1]),
                top=top_name,
                error=f"simple-exist: {seg!r} not found in {current_mod}",
                nodes=nodes,
                fail_segment=seg,
            )

        is_last = i == len(parts) - 1
        node: Dict[str, Any] = {
            "segment": seg,
            "role": "inst" if not is_last else "leaf",
            "module": current_mod,
            "file": fpath,
            "hit_file": fpath,
            "found": True,
        }
        if is_last:
            node["kind"] = "inst"
        nodes.append(node)

        if is_last:
            return _result(
                ok=True,
                hierarchy=".".join(parts),
                top=top_name,
                error="",
                nodes=nodes,
            )

        child_mod = _child_module_for_inst(session, body, seg)
        if not child_mod:
            return _result(
                ok=False,
                hierarchy=".".join(parts[: i + 1]),
                top=top_name,
                error=f"simple-exist: no child module for inst {seg!r}",
                nodes=nodes,
                fail_segment=seg,
            )
        current_mod = child_mod
        child_files = session.module_index.get(child_mod) or []
        current_file = str(child_files[0]) if child_files else ""

    return _result(
        ok=True,
        hierarchy=".".join(parts),
        top=top_name,
        error="",
        nodes=nodes,
    )


def _result(
    *,
    ok: bool,
    hierarchy: str,
    top: str,
    error: str,
    nodes: List[Dict[str, Any]],
    fail_segment: str = "",
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "ok": ok,
        "top": top,
        "hierarchy": hierarchy,
        "hierarchy_input": hierarchy,
        "error": error,
        "ambiguous": False,
        "nodes": nodes,
        "candidates": [],
        "simple_exist": True,
    }
    if fail_segment:
        out["fail_segment"] = fail_segment
    return out