#!/usr/bin/env python3
"""
Structural connectivity between run_conn_check groups a (fanout) and b (fanin).

  python3 hier_conn.py \\
    --config run.json \\
    --map essential.modules.json \\
    --resolve hier_resolve.json \\
    -o hier_conn.json

Seeds = hierarchies with status ok / ok_needs_detail in --resolve only.
See docs/COI.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pyhirewalk.conn.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
