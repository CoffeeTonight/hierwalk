"""One-off debug tracing for wire+inst ``u_b`` / scoped child-FL selection misses.

Enable with ``HIERWALK_UBPAT_LOG=1``.  Optional::

    HIERWALK_UBPAT_LEAF=u_b          # inst leaf filter (default u_b)
    HIERWALK_UBPAT_MODULE=ModA,ModB # extra module names to trace
    HIERWALK_UBPAT_SEARCH=1          # log every module-search pool (verbose)

Grep logs::

    grep 'UBPAT-' run.log
    grep 'UBPAT-MAP-REG\\|UBPAT-RANK\\|UBPAT-LISTING' run.log
    grep 'UBPAT-SEARCH\\|UBPAT-PARSE-EP' run.log
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


def _env_bool(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def ubpat_enabled() -> bool:
    return _env_bool("HIERWALK_UBPAT_LOG", default=False)


def ubpat_inst_leaf() -> str:
    return os.environ.get("HIERWALK_UBPAT_LEAF", "u_b").strip() or "u_b"


def _leaf_token(text: str) -> str:
    if not text:
        return ""
    return text.split("[", 1)[0].split(".", 1)[0]


def ubpat_search_all() -> bool:
    """Log every module-search pool when ``HIERWALK_UBPAT_SEARCH=1``."""
    return _env_bool("HIERWALK_UBPAT_SEARCH", default=False)


def ubpat_module_watch() -> frozenset[str]:
    """Extra module names to trace (``HIERWALK_UBPAT_MODULE``, comma-separated)."""
    raw = os.environ.get("HIERWALK_UBPAT_MODULE", "").strip()
    if not raw or raw in {"*", "all"}:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def ubpat_relevant(
    *,
    inst_leaf: str = "",
    target_path: str = "",
    remainder: str = "",
    parent_path: str = "",
    scope_anchor: str = "",
    module_name: str = "",
) -> bool:
    if not ubpat_enabled():
        return False
    leaf = ubpat_inst_leaf()
    if _leaf_token(inst_leaf) == leaf:
        return True
    if _leaf_token(remainder) == leaf:
        return True
    for path in (target_path, parent_path):
        if not path:
            continue
        if path.endswith(f".{leaf}") or f".{leaf}." in path or f".{leaf}[" in path:
            return True
    return False


def ubpat_search_relevant(
    *,
    module_name: str = "",
    target_module: str = "",
    inst_leaf: str = "",
    target_path: str = "",
    remainder: str = "",
    parent_path: str = "",
    scope_anchor: str = "",
) -> bool:
    if not ubpat_enabled():
        return False
    if ubpat_search_all():
        return True
    watch = ubpat_module_watch()
    seek = module_name or target_module
    if seek and seek in watch:
        return True
    return ubpat_relevant(
        inst_leaf=inst_leaf,
        target_path=target_path,
        remainder=remainder,
        parent_path=parent_path,
        scope_anchor=scope_anchor,
        module_name=module_name,
    )


def _fmt_path(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).name
    except (TypeError, ValueError):
        return str(path)


def _fmt_paths(paths: Sequence[str], *, limit: int = 8) -> str:
    names = [_fmt_path(p) for p in paths if p]
    if len(names) > limit:
        tail = len(names) - limit
        names = names[:limit] + [f"+{tail}"]
    return ",".join(names) if names else "-"


def ubpat_log(tag: str, **fields: Any) -> None:
    if not ubpat_enabled():
        return
    parts = [f"[hier-walk ubp] UBPAT-{tag}"]
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            text = _fmt_paths(list(value))
        elif key.endswith("_file") or key in {
            "anchor",
            "scope_anchor",
            "listing",
            "avoid",
            "seek",
            "target_module",
        }:
            text = _fmt_path(str(value)) if value else ""
        else:
            text = str(value)
        if text:
            parts.append(f"{key}={text}")
    sys.stderr.write(" ".join(parts) + "\n")
    sys.stderr.flush()


def ubpat_log_mapping(tag: str, mapping: Mapping[str, Any]) -> None:
    ubpat_log(tag, **dict(mapping))