from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


@dataclass
class InstanceEdge:
    inst_name: str
    child_module: str
    param_overrides: Dict[str, str] = field(default_factory=dict)


@dataclass
class ModuleRecord:
    module_name: str
    file_path: str
    body: str = ""
    raw_params: Dict[str, str] = field(default_factory=dict)
    instances: List[InstanceEdge] = field(default_factory=list)
    needs_generate_fold: bool = False
    is_blackbox: bool = False
    is_interface: bool = False
    stop_reason: str = ""

    def __getstate__(self) -> dict:
        """Omit module bodies from pickle; live records keep lazy-filled bodies."""
        state = self.__dict__.copy()
        state["body"] = ""
        return state


@dataclass
class ElabIndex:
    """Pre-built hierarchy lookups for connectivity / cone (built once per elab)."""

    rows: List[FlatRow]
    rows_by_path: Dict[str, FlatRow]
    child_by_parent_leaf: Dict[Tuple[str, str], str]
    depth_by_path: Dict[str, int]

    @classmethod
    def from_rows(cls, rows: Sequence[FlatRow]) -> "ElabIndex":
        rows_list = list(rows)
        return cls.from_rows_by_path({r.full_path: r for r in rows_list}, rows=rows_list)

    @classmethod
    def from_rows_by_path(
        cls,
        rows_by_path: Mapping[str, FlatRow],
        *,
        rows: Optional[List[FlatRow]] = None,
    ) -> "ElabIndex":
        """Build index lookups; reuse *rows_by_path* dict when already materialized."""
        rows_list = rows if rows is not None else list(rows_by_path.values())
        shared_map = (
            rows_by_path
            if isinstance(rows_by_path, dict)
            else dict(rows_by_path)
        )
        child_by_parent_leaf: Dict[Tuple[str, str], str] = {}
        depth_by_path: Dict[str, int] = {}
        for row in rows_list:
            depth_by_path[row.full_path] = row.depth
            if row.parent_path:
                child_by_parent_leaf[(row.parent_path, row.inst_leaf)] = row.full_path
        return cls(
            rows=rows_list,
            rows_by_path=shared_map,
            child_by_parent_leaf=child_by_parent_leaf,
            depth_by_path=depth_by_path,
        )

    def extend_rows(self, rows_by_path: Mapping[str, "FlatRow"]) -> "ElabIndex":
        """Append new hierarchy rows without rebuilding existing lookups."""
        changed = False
        for path, row in rows_by_path.items():
            if path in self.rows_by_path:
                continue
            self.rows_by_path[path] = row
            self.rows.append(row)
            self.depth_by_path[path] = row.depth
            if row.parent_path:
                self.child_by_parent_leaf[(row.parent_path, row.inst_leaf)] = path
            changed = True
        return self


@dataclass
class FlatRow:
    full_path: str
    inst_leaf: str
    module: str
    depth: int
    parent_path: Optional[str]
    file: str
    stop_reason: str = ""
    via_filelist: str = ""
    filelist_chain: str = ""
    param_ctx: Dict[str, str] = field(default_factory=dict)
    param_ctx_folded: bool = False
    refine_status: str = ""
    activation: str = ""
    walk_note: str = ""


@dataclass
class ElabNode:
    """Elaborated instance tree node (dict-stitched from :class:`DesignIndex`)."""

    inst_name: str
    module: str
    full_path: str
    file_path: str
    param_ctx: Dict[str, str] = field(default_factory=dict)
    stop_reason: str = ""
    children: List[ElabNode] = field(default_factory=list)


@dataclass
class FilelistLinkInfo:
    path: str
    exists: bool
    chain: str
    parent: str
    include_kind: str


@dataclass
class PathChainLink:
    """One hop in a hierarchy path mapped to RTL sources."""

    hierarchy_path: str
    inst: str
    module: str
    role: str
    rtl_file: str
    inst_decl_file: str = ""
    port_name: str = ""
    port_line: int = 0
    via_filelist: str = ""
    filelist_chain: str = ""
    inst_decl_via_filelist: str = ""
    inst_decl_filelist_chain: str = ""


@dataclass
class PortInfo:
    """One port declaration (may materialize to many index names)."""

    base_name: str
    names: List[str]
    dim_specs: List[str] = field(default_factory=list)
    line: int = 0
    decl: str = ""
    param_note: str = ""


@dataclass
class ConnectEndpoint:
    spec: str
    inst_path: str
    port_name: str = ""
    module: str = ""
    port_found: bool = False


@dataclass
class ConnectHop:
    kind: str
    detail: str


@dataclass
class ConnectResult:
    endpoint_a: ConnectEndpoint
    endpoint_b: ConnectEndpoint
    connected: bool
    mode: str
    hops: List[ConnectHop] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    note: str = ""
    check_id: str = ""
    sub_results: Tuple["ConnectResult", ...] = ()
    waypoint_events: Tuple[Any, ...] = ()
    connected_text: Optional[bool] = None
    connected_logical: Optional[bool] = None
    logical_notes: List[str] = field(default_factory=list)
    walk_notes: List[str] = field(default_factory=list)
    coi_walk: Any = None


@dataclass
class SearchHit:
    full_path: str
    matched_name: str
    module: str
    depth: int
    file: str
    match_kind: str
    stop_reason: str = ""
    via_filelist: str = ""
    filelist_chain: str = ""
    port_name: str = ""
    port_found: bool = False
    port_line: int = 0
    port_decl: str = ""
    port_param_note: str = ""
    path_chain: List[PathChainLink] = field(default_factory=list)