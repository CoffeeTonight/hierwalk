"""
Cross-platform path normalization (Linux / Windows / macOS).

- **DB / module_ref / DQL:** forward-slash absolute paths (portable, stable LIKE).
- **slang / pyslang filelists:** forward-slash absolute paths (EDA convention on Windows).
- **Comparisons:** case-insensitive on Windows.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import re
import sys
from pathlib import Path
from typing import Mapping, Optional, Union

PathLike = Union[str, Path]

_ENV_REF_PAT = re.compile(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_LIBC = None


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


def _libc() -> Optional[ctypes.CDLL]:
    global _LIBC
    if _LIBC is not None:
        return _LIBC
    if is_windows():
        return None
    try:
        _LIBC = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    except (AttributeError, OSError, TypeError):
        _LIBC = None
    return _LIBC


def _libc_environ() -> dict[str, str]:
    """
    Read the process environment from libc (POSIX).

    C ``setenv`` after Python started does not refresh ``os.environ``; libc is
    the source of truth for those variables.
    """
    libc = _libc()
    if libc is None:
        return {}
    try:
        environ = ctypes.POINTER(ctypes.c_char_p).in_dll(libc, "environ")
    except (AttributeError, ValueError):
        return {}
    if not environ:
        return {}
    out: dict[str, str] = {}
    idx = 0
    while environ[idx]:
        raw = environ[idx]
        if raw is None:
            break
        entry = raw.decode("utf-8", errors="surrogateescape")
        idx += 1
        if "=" not in entry:
            continue
        key, _, value = entry.partition("=")
        out[key] = value
    return out


def _libc_getenv(name: str) -> Optional[str]:
    libc = _libc()
    if libc is None:
        return None
    try:
        libc.getenv.argtypes = [ctypes.c_char_p]
        libc.getenv.restype = ctypes.c_char_p
        raw = libc.getenv(name.encode())
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    if raw is None:
        return None
    return raw.decode("utf-8", errors="surrogateescape")


def merge_environ(extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """libc + ``os.environ`` + optional overrides (JSON ``env`` wins last)."""
    merged = dict(_libc_environ())
    merged.update(os.environ)
    if extra:
        for key, value in extra.items():
            key_s = str(key)
            if value is None:
                merged.pop(key_s, None)
            else:
                merged[key_s] = str(value)
    return merged


def lookup_env_var(name: str, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Resolve one variable from overrides, Python env, then libc ``getenv``."""
    if env is not None and name in env:
        return env[name]
    hit = os.environ.get(name)
    if hit is not None:
        return hit
    return _libc_getenv(name)


def unexpanded_path_vars(raw: str) -> tuple[str, ...]:
    """Return env var names still present as ``$VAR`` / ``${VAR}`` after expansion."""
    s = normalize_filelist_token(raw)
    if "$" not in s and "%" not in s:
        return ()
    names: list[str] = []
    for match in _ENV_REF_PAT.finditer(s):
        name = match.group(1) or match.group(2)
        if name and name not in names:
            names.append(name)
    return tuple(names)


def expand_path_vars(
    raw: str,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """
    Expand only referenced ``$VAR`` / ``${VAR}`` (and Windows ``%VAR%``).

    Lookup order per variable: merged overrides → ``os.environ`` → libc ``getenv``.
    """
    s = normalize_filelist_token(raw)
    if "$" not in s and "%" not in s:
        return s

    env_map = merge_environ(env)

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        value = lookup_env_var(name, env_map)
        if value is None:
            return match.group(0)
        return str(value)

    s = _ENV_REF_PAT.sub(_replace, s)
    if is_windows() or "%" in s:
        return os.path.expandvars(s)
    return s


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