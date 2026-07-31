#!/usr/bin/env python3
"""
pyslang structural connectivity (generate-aware + bit-select meta).

Uses run JSON env + defines for compile context.

  python3 hier_pyslang.py \\
    --config examples/ibex/run_ibex.json \\
    -o examples/ibex/work/hier_pyslang.json

See docs/COI.md (regex ceiling) and examples/ibex/README.md.
hier_conn (regex) remains the lightweight engine; this is the precision path.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
# Avoid shadowing if a future hier_pyslang package name conflicts
_root_s = str(_ROOT)
while _root_s in sys.path:
    sys.path.remove(_root_s)
if "" in sys.path:
    sys.path.remove("")
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))
sys.path.append(_root_s)

from pyhirewalk.conn.pyslang_app import HierPyslangApp, main  # noqa: E402

__all__ = ["HierPyslangApp", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
