"""EDA filelist expansion (VCS -f/-F) and slang-safe flatten."""

from __future__ import annotations

from pyhirewalk.filelist.cwd import resolve_index_cwd
from pyhirewalk.filelist.envexpand import expand_eda_env, find_env_refs
from pyhirewalk.filelist.expand import (
    FilelistResult,
    build_slang_filelist_lines,
    expand_filelist,
    write_slang_filelist,
)

__all__ = [
    "FilelistResult",
    "build_slang_filelist_lines",
    "expand_eda_env",
    "expand_filelist",
    "find_env_refs",
    "resolve_index_cwd",
    "write_slang_filelist",
]
