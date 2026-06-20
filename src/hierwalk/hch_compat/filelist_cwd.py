"""Resolve the EDA run directory used for ``-F`` filelist semantics."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

_ENV_INDEX_CWD = "HCH_INDEX_CWD"


def resolve_index_cwd(
    top_filelist: Union[str, Path],
    index_cwd: Optional[Union[str, Path]] = None,
    env: Optional[dict] = None,
) -> Path:
    """
    Directory for ``-F`` nested paths and in-file RTL lines.

    Priority: explicit ``index_cwd`` → ``HCH_INDEX_CWD`` env → parent of top ``.f``.
    """
    from hierwalk.hch_compat.platform_paths import resolve_path

    if index_cwd is not None and str(index_cwd).strip():
        return resolve_path(index_cwd)
    env_map = env if env is not None else os.environ
    raw = str(env_map.get(_ENV_INDEX_CWD, "") or "").strip()
    if raw:
        return resolve_path(raw)
    return resolve_path(Path(top_filelist).parent)