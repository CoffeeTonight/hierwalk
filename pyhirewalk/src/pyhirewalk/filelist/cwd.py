"""Resolve the EDA run directory used for ``-F`` filelist semantics."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional, Union

from pyhirewalk.filelist.paths import resolve_path

# Prefer project env; accept legacy hierwalk/hch name as fallback.
_ENV_KEYS = ("PYHIREWALK_INDEX_CWD", "HCH_INDEX_CWD", "HIERWALK_INDEX_CWD")


def resolve_index_cwd(
    top_filelist: Union[str, Path],
    index_cwd: Optional[Union[str, Path]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Path:
    """
    Directory for ``-F`` nested paths and relative lines inside those lists.

    Priority: explicit ``index_cwd`` → env → parent of top ``.f``.
    """
    if index_cwd is not None and str(index_cwd).strip():
        return resolve_path(index_cwd)
    env_map = env if env is not None else os.environ
    for key in _ENV_KEYS:
        raw = str(env_map.get(key, "") or "").strip()
        if raw:
            return resolve_path(raw)
    return resolve_path(Path(top_filelist).parent)
