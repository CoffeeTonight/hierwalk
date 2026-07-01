"""Logical-conn: bit-precise COI scan + hierarchy search."""

from __future__ import annotations

from hierwalk.connect.logical.pair import connect_pair
from hierwalk.connect.logical.scan import ModuleConnectIndex, build_module_connect_index
from hierwalk.connect.logical.search import _bidirectional_coi, _forward_coi_to_scope

__all__ = [
    "ModuleConnectIndex",
    "_bidirectional_coi",
    "_forward_coi_to_scope",
    "build_module_connect_index",
    "connect_pair",
]