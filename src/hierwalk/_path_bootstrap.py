"""Prepend checkout ``src/`` on ``sys.path`` from this package file location."""

from __future__ import annotations

import sys
from pathlib import Path


def ensure_src_on_sys_path(anchor: str | Path | None = None) -> Path | None:
    here = Path(anchor or __file__).resolve()
    if here.is_file():
        candidates = [here.parent.parent, *here.parents]
    else:
        candidates = [here, *here.parents]
    for base in candidates:
        src = base if (base / "hierwalk" / "__init__.py").is_file() else base / "src"
        if (src / "hierwalk" / "__init__.py").is_file():
            entry = str(src.resolve())
            if entry not in sys.path:
                sys.path.insert(0, entry)
            return Path(entry)
    return None