"""Parse assign, FF, and instance port-map connectivity within a module body."""

from __future__ import annotations

import sys

from hierwalk.connect.logical.scan import core as _core
from hierwalk.connect.logical.scan.types import (
    BindRecord,
    ConnectEdgeProv,
    ModuleConnectIndex,
    binds_digest,
)

_pkg = sys.modules[__name__]
for _name in dir(_core):
    if not _name.startswith("__"):
        setattr(_pkg, _name, getattr(_core, _name))
_pkg.BindRecord = BindRecord
_pkg.ConnectEdgeProv = ConnectEdgeProv
_pkg.ModuleConnectIndex = ModuleConnectIndex
_pkg.binds_digest = binds_digest
del _pkg, _name, _core