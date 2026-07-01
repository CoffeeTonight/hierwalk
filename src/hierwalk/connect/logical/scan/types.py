"""Module connect index datatypes (logical COI scan)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Sequence, Set, Tuple


@dataclass
class BindRecord:
    """``bind target cell inst (...);`` attached to *target* module."""

    cell: str
    inst_leaf: str
    ports: List[Tuple[str, str]]


def binds_digest(binds: Sequence[BindRecord]) -> str:
    """Stable digest of bind records targeting one module."""
    if not binds:
        return "0" * 16
    hasher = hashlib.sha256()
    for rec in sorted(binds, key=lambda b: (b.cell, b.inst_leaf)):
        hasher.update(rec.cell.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(rec.inst_leaf.encode("utf-8"))
        hasher.update(b"\0")
        for port, expr in sorted(rec.ports):
            hasher.update(port.encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(expr.encode("utf-8"))
            hasher.update(b"\0")
    return hasher.hexdigest()[:16]


@dataclass(frozen=True)
class ConnectEdgeProv:
    line: int
    kind: str


@dataclass
class ModuleConnectIndex:
    """Pre-compressed intra-module connectivity + fast hierarchy hooks."""

    inst_ports: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    net_rep: Dict[str, str] = field(default_factory=dict)
    rep_adj: Dict[str, Set[str]] = field(default_factory=dict)
    net_to_children: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    expr_roots: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    hier_links: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    hier_ref_targets: Dict[Tuple[str, str], Set[str]] = field(default_factory=dict)
    edge_prov: Dict[Tuple[str, str], ConnectEdgeProv] = field(default_factory=dict)
    inst_stmt_lines: Dict[str, int] = field(default_factory=dict)
    ff_net_lines: Dict[str, int] = field(default_factory=dict)
    ff_d_roots: FrozenSet[str] = field(default_factory=frozenset)
    ff_q_roots: FrozenSet[str] = field(default_factory=frozenset)
    vector_bases: FrozenSet[str] = field(default_factory=frozenset)
    vector_scalar_rep: Dict[str, str] = field(default_factory=dict)
    bit_precise_bases: FrozenSet[str] = field(default_factory=frozenset)
    resolve_param_dims: bool = True

    def copy(self) -> ModuleConnectIndex:
        """Shallow copy of mutable fields (safe before in-place bind folding)."""
        return ModuleConnectIndex(
            inst_ports={k: list(v) for k, v in self.inst_ports.items()},
            net_rep=dict(self.net_rep),
            rep_adj={k: set(v) for k, v in self.rep_adj.items()},
            net_to_children={k: list(v) for k, v in self.net_to_children.items()},
            expr_roots=dict(self.expr_roots),
            hier_links={k: list(v) for k, v in self.hier_links.items()},
            hier_ref_targets={
                k: set(v) for k, v in self.hier_ref_targets.items()
            },
            edge_prov=dict(self.edge_prov),
            inst_stmt_lines=dict(self.inst_stmt_lines),
            ff_net_lines=dict(self.ff_net_lines),
            ff_d_roots=self.ff_d_roots,
            ff_q_roots=self.ff_q_roots,
            vector_bases=self.vector_bases,
            vector_scalar_rep=dict(self.vector_scalar_rep),
            bit_precise_bases=self.bit_precise_bases,
            resolve_param_dims=self.resolve_param_dims,
        )