"""Path normalization for filelists and slang command files."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Union

PathLike = Union[str, Path]


def resolve_path(path: PathLike) -> Path:
    """Expand ``~`` and resolve to an absolute path when possible."""
    p = Path(path).expanduser()
    if not str(p):
        return p
    try:
        return p.resolve()
    except (OSError, RuntimeError):
        return p.absolute()


def path_to_posix(path: PathLike) -> str:
    """Absolute path with forward slashes (stable for DB / slang)."""
    return resolve_path(path).as_posix()


def path_to_slang(path: PathLike) -> str:
    """Path string for slang/pyslang command files."""
    return path_to_posix(path)


def normalize_filelist_token(raw: str) -> str:
    """Strip quotes from a filelist token."""
    return raw.strip().strip('"').strip("'").strip()


def is_windows() -> bool:
    return sys.platform == "win32"
