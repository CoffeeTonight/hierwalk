"""Read only tree-scoped RTL files."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Sequence

LogFn = Optional[Callable[[str], None]]


def load_scoped_files(
    paths: Sequence[str],
    *,
    on_log: LogFn = None,
) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in paths:
        key = str(Path(raw).resolve())
        if not key or key in out:
            continue
        try:
            out[key] = Path(key).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            out[key] = ""
    if on_log:
        on_log(f"scoped files={len(out)}")
    return out