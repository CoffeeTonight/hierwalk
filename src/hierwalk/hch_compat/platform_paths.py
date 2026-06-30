"""
Cross-platform path normalization (Linux / Windows / macOS).

- **DB / module_ref / DQL:** forward-slash absolute paths (portable, stable LIKE).
- **slang / pyslang filelists:** forward-slash absolute paths (EDA convention on Windows).
- **Comparisons:** case-insensitive on Windows.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Mapping, Optional, Union

PathLike = Union[str, Path]


def is_windows() -> bool:
    return sys.platform == "win32"


def resolve_path(path: PathLike) -> Path:
    """Expand ``~``, normalize separators, resolve to absolute when possible."""
    p = Path(path).expanduser()
    if not str(p):
        return p
    try:
        return p.resolve()
    except (OSError, RuntimeError):
        return p.absolute()


def path_to_posix(path: PathLike) -> str:
    """Canonical stored path: absolute, ``/`` separators."""
    p = resolve_path(path)
    return p.as_posix()


def path_to_slang(path: PathLike) -> str:
    """Path string for slang/pyslang command files (always forward slashes)."""
    return path_to_posix(path)


def path_to_db(path: PathLike) -> str:
    """Path string for SQLite ``files.filepath`` and ``module_ref`` prefix."""
    return path_to_posix(path)


def path_key(path: PathLike) -> str:
    """Key for equality / dict lookups (case-fold on Windows)."""
    s = path_to_db(path)
    return s.casefold() if is_windows() else s


def paths_equal(a: PathLike, b: PathLike) -> bool:
    return path_key(a) == path_key(b)


def path_contains(haystack: PathLike, needle: str) -> bool:
    """Substring test for definition-file heuristics (case-fold on Windows)."""
    h = path_key(haystack)
    n = needle.replace("\\", "/").casefold() if is_windows() else needle.replace("\\", "/")
    return n in h


def normalize_filelist_token(raw: str) -> str:
    """Strip quotes; keep token usable with :class:`Path` on any OS."""
    return raw.strip().strip('"').strip("'").strip()


def merge_environ(extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Process environment merged with optional overrides (JSON ``env`` block, etc.)."""
    merged = dict(os.environ)
    if extra:
        for key, value in extra.items():
            if value is None:
                merged.pop(str(key), None)
            else:
                merged[str(key)] = str(value)
    return merged


def expand_path_vars(
    raw: str,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """
    Expand ``$VAR`` / ``${VAR}`` (and Windows ``%VAR%``) using the process environment.

    Used for RUN.json paths (``filelist``, ``index_cwd``, …) and Verilog filelist lines.
    Longer variable names are substituted before shorter ones (``$PROJ_ROOT`` before ``$PROJ``).
    """
    s = normalize_filelist_token(raw)
    env_map = merge_environ(env)
    for key in sorted(env_map, key=lambda name: -len(name)):
        value = env_map[key]
        s = s.replace(f"${{{key}}}", value).replace(f"${key}", value)
    return os.path.expandvars(s)


def normalize_dql_path_pattern(pattern: str) -> str:
    """DQL glob on ``file`` / ``module_ref``: accept ``\\`` or ``/`` in queries."""
    return pattern.replace("\\", "/")


def browser_auto_open_default() -> bool:
    """Whether ``hch-web`` should open a browser tab without an explicit flag."""
    rel = platform.release().lower()
    if "proot" in rel:
        return False
    try:
        with open("/proc/version", encoding="utf-8", errors="ignore") as fh:
            if "proot" in fh.read().lower():
                return False
    except OSError:
        pass
    return True