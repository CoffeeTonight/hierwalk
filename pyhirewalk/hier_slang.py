#!/usr/bin/env python3
"""
Scoped pyslang structural connectivity (HierSlangApp).

  python3 hier_slang.py \\
    --config run.json \\
    --map essential.modules.json \\
    --resolve hier_resolve.json \\
    --files ibex.f \\
    --top ibex_core \\
    -o hier_slang.json
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pyhirewalk.conn.slang import HierSlangApp, main  # noqa: E402

__all__ = ["HierSlangApp", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
