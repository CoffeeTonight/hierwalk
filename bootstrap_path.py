"""Resolve hierwalk checkout ``src/`` from an executing file and set PYTHONPATH."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping


def src_root_from(anchor: str | Path) -> Path:
    """Return absolute ``src/`` that contains the ``hierwalk`` package."""
    path = Path(anchor).resolve()
    if path.is_file():
        path = path.parent
    for base in (path, *path.parents):
        src = base / "src"
        if (src / "hierwalk" / "__init__.py").is_file():
            return src
    raise RuntimeError(f"cannot locate hierwalk src/ from anchor {anchor!r}")


def ensure_src_on_sys_path(anchor: str | Path | None = None) -> Path:
    """Prepend checkout ``src/`` to ``sys.path`` when discovered from *anchor*."""
    if anchor is None:
        anchor = Path(__file__).resolve()
    src = src_root_from(anchor)
    entry = str(src)
    if entry not in sys.path:
        sys.path.insert(0, entry)
    return src


def pythonpath_for(
    src: str | Path,
    *,
    merge: bool = True,
    base: Mapping[str, str] | None = None,
) -> str:
    """Build a PYTHONPATH string with checkout ``src/`` first."""
    entry = str(Path(src).resolve())
    env = dict(base if base is not None else os.environ)
    if not merge:
        return entry
    existing = env.get("PYTHONPATH", "")
    if not existing:
        return entry
    parts = [p for p in existing.split(os.pathsep) if p]
    if entry in parts:
        return existing
    return os.pathsep.join([entry, existing])


def export_shell(anchor: str | Path | None = None) -> str:
    """Shell snippet: ``export PYTHONPATH=...`` from *anchor* (default: this file)."""
    src = src_root_from(anchor or Path(__file__).resolve())
    value = pythonpath_for(src, merge=True)
    quoted = value.replace("'", "'\\''")
    return f"export PYTHONPATH='{quoted}'"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--anchor",
        default=str(Path(__file__).resolve()),
        help="file whose location determines the checkout src/ directory",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        help="print absolute src/ path",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="print export PYTHONPATH=... for shell eval",
    )
    args = parser.parse_args(argv)
    src = src_root_from(args.anchor)
    if args.export:
        print(export_shell(args.anchor), end="")
    elif args.print:
        print(src)
    else:
        print(src)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())