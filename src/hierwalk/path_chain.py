"""Map hierarchy paths to RTL files, instances, and ports."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence

from hierwalk.index import DesignIndex
from hierwalk.hierarchy_log import format_path_link_provenance
from hierwalk.models import FlatRow, PathChainLink, SearchHit
from hierwalk.path_refine import PathRefineResult, refine_param_ctx_for_path


def _row_by_path(rows: Sequence[FlatRow]) -> Dict[str, FlatRow]:
    return {row.full_path: row for row in rows}


def _link_filelists(index: DesignIndex, rtl_file: str) -> tuple[str, str]:
    if not rtl_file:
        return "", ""
    return index.filelist_for(rtl_file), index.filelist_chain_for(rtl_file)


def build_path_chain(
    index: DesignIndex,
    inst_path: str,
    rows: Sequence[FlatRow],
    *,
    refine: Optional[PathRefineResult] = None,
    port_name: str = "",
    port_line: int = 0,
    port_module: str = "",
    port_via_filelist: str = "",
    port_filelist_chain: str = "",
) -> List[PathChainLink]:
    """Build inst/port → RTL file mapping along one hierarchy path."""
    row_map = _row_by_path(rows)
    parts = inst_path.split(".")
    if not parts:
        return []

    links: List[PathChainLink] = []
    if refine and refine.steps:
        cum: List[str] = []
        for i, step in enumerate(refine.steps):
            cum.append(step.inst_leaf)
            hierarchy_path = ".".join(cum)
            row = row_map.get(hierarchy_path)
            via, chain = _link_filelists(index, step.file)
            if row:
                via = row.via_filelist or via
                chain = row.filelist_chain or chain

            if i == 0:
                links.append(
                    PathChainLink(
                        hierarchy_path=hierarchy_path,
                        inst=step.inst_leaf,
                        module=step.child_module,
                        role="root",
                        rtl_file=step.file,
                        via_filelist=via,
                        filelist_chain=chain,
                    )
                )
                continue

            parent_rec = index.get_module(step.module)
            parent_file = parent_rec.file_path if parent_rec else ""
            parent_via, parent_chain = _link_filelists(index, parent_file)
            links.append(
                PathChainLink(
                    hierarchy_path=hierarchy_path,
                    inst=step.inst_leaf,
                    module=step.child_module,
                    role="instance",
                    rtl_file=step.file,
                    inst_decl_file=parent_file,
                    via_filelist=via,
                    filelist_chain=chain,
                    inst_decl_via_filelist=parent_via,
                    inst_decl_filelist_chain=parent_chain,
                )
            )
    else:
        for i, seg in enumerate(parts):
            hierarchy_path = ".".join(parts[: i + 1])
            row = row_map.get(hierarchy_path)
            if row is None:
                continue
            role = "root" if i == 0 else "instance"
            parent_file = ""
            parent_via = ""
            parent_chain = ""
            if i > 0:
                parent = row_map.get(".".join(parts[:i]))
                if parent:
                    parent_file = parent.file
                    parent_via = parent.via_filelist
                    parent_chain = parent.filelist_chain
            links.append(
                PathChainLink(
                    hierarchy_path=hierarchy_path,
                    inst=seg,
                    module=row.module,
                    role=role,
                    rtl_file=row.file,
                    inst_decl_file=parent_file,
                    via_filelist=row.via_filelist,
                    filelist_chain=row.filelist_chain,
                    inst_decl_via_filelist=parent_via,
                    inst_decl_filelist_chain=parent_chain,
                )
            )

    if port_name:
        leaf = row_map.get(inst_path)
        rtl_file = leaf.file if leaf else ""
        via = port_via_filelist or (leaf.via_filelist if leaf else "")
        chain = port_filelist_chain or (leaf.filelist_chain if leaf else "")
        if not via and rtl_file:
            via, chain = _link_filelists(index, rtl_file)
        module = port_module or (leaf.module if leaf else "")
        links.append(
            PathChainLink(
                hierarchy_path=f"{inst_path}.{port_name}",
                inst=port_name,
                module=module,
                role="port",
                rtl_file=rtl_file,
                port_name=port_name,
                port_line=port_line,
                via_filelist=via,
                filelist_chain=chain,
            )
        )
    return links


def attach_path_chains(
    hits: Sequence[SearchHit],
    index: DesignIndex,
    rows: Sequence[FlatRow],
    *,
    top: str = "",
    refine_paths: bool = True,
    refine_cache: Optional[Dict[str, PathRefineResult]] = None,
) -> List[SearchHit]:
    """Populate :attr:`SearchHit.path_chain` for hierarchy/port search hits."""
    row_map = _row_by_path(rows)
    cached = refine_cache if refine_cache is not None else {}
    top_name = top or (rows[0].full_path.split(".", 1)[0] if rows else "")

    out: List[SearchHit] = []
    for hit in hits:
        inst_path = hit.full_path
        if hit.port_name and hit.full_path.endswith(f".{hit.port_name}"):
            inst_path = hit.full_path[: -(len(hit.port_name) + 1)]

        refine: Optional[PathRefineResult] = None
        if refine_paths and top_name and inst_path:
            if inst_path not in cached:
                cached[inst_path] = refine_param_ctx_for_path(
                    index, top_name, inst_path
                )
            refine = cached[inst_path]

        hit.path_chain = build_path_chain(
            index,
            inst_path,
            rows,
            refine=refine if refine and refine.ok else None,
            port_name=hit.port_name,
            port_line=hit.port_line,
            port_module=hit.module,
            port_via_filelist=hit.via_filelist,
            port_filelist_chain=hit.filelist_chain,
        )
        if not hit.path_chain and inst_path in row_map:
            row = row_map[inst_path]
            hit.path_chain = build_path_chain(index, inst_path, rows)
            if hit.path_chain and not hit.via_filelist:
                hit.via_filelist = row.via_filelist
                hit.filelist_chain = row.filelist_chain
        out.append(hit)
    return out


def format_path_chain_compact(links: Sequence[PathChainLink]) -> str:
    """Single TSV-safe field encoding the path chain."""
    chunks: List[str] = []
    for link in links:
        fields = [
            link.role,
            link.inst,
            link.module,
            link.rtl_file,
            link.inst_decl_file,
            str(link.port_line) if link.port_line else "",
            link.via_filelist,
            link.filelist_chain,
            link.inst_decl_via_filelist,
        ]
        chunks.append("|".join(fields))
    return ";".join(chunks)


def format_path_chain_report(links: Sequence[PathChainLink]) -> List[str]:
    """Human-readable report lines for one hit."""
    lines: List[str] = []
    for link in links:
        rtl_name = Path(link.rtl_file).name if link.rtl_file else "(unknown)"
        if link.role == "root":
            lines.append(
                f"    {link.inst:<16} root   module={link.module}  rtl={rtl_name}"
            )
        elif link.role == "instance":
            decl_name = (
                Path(link.inst_decl_file).name if link.inst_decl_file else "(unknown)"
            )
            lines.append(
                f"    {link.inst:<16} inst   module={link.module}  "
                f"rtl={rtl_name}  decl_in={decl_name}"
            )
        else:
            line = f":{link.port_line}" if link.port_line else ""
            lines.append(
                f"    {link.port_name:<16} port   module={link.module}  "
                f"rtl={rtl_name}{line}"
            )
        prov = format_path_link_provenance(link)
        if prov:
            lines.append(f"      {'':16}       {prov}")
    return lines