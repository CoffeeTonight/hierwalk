"""EDA filelist expansion (VCS -f/-F) and slang-safe flatten."""

from __future__ import annotations

from pyhirewalk.filelist.cwd import resolve_index_cwd
from pyhirewalk.filelist.expand import (
    FilelistResult,
    build_slang_filelist_lines,
    expand_filelist,
    write_slang_filelist,
)

__all__ = [
    "FilelistResult",
    "build_slang_filelist_lines",
    "expand_filelist",
    "resolve_index_cwd",
    "write_slang_filelist",
]
