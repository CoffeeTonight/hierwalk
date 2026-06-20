"""Scan preprocessed RTL and flatten hierarchy."""

from __future__ import annotations

from typing import Dict, List, Optional

from hierwalk.elab import elaborate, flatten as _flatten
from hierwalk.index import DesignIndex, scan_preprocessed
from hierwalk.library_scan import scan_library_modules
from hierwalk.models import ElabNode, FlatRow, ModuleRecord


def scan_all_preprocessed(preprocessed: Dict[str, str]) -> Dict[str, ModuleRecord]:
    return DesignIndex.build(preprocessed).modules


def merge_library_stubs(
    modules: Dict[str, ModuleRecord],
    stubs: Dict[str, ModuleRecord],
) -> None:
    for name, stub in stubs.items():
        if name not in modules:
            modules[name] = stub


def build_index(
    preprocessed: Dict[str, str],
    *,
    library_files: Optional[List[str]] = None,
    library_dirs: Optional[List[str]] = None,
    libexts: Optional[List[str]] = None,
    ignore_paths: Optional[List[str]] = None,
    ignore_path_files: Optional[List[str]] = None,
    ignore_modules: Optional[List[str]] = None,
    ignore_filelists: Optional[List[str]] = None,
    jobs: int = 0,
    file_via_filelist: Optional[Dict[str, str]] = None,
    file_filelist_chain: Optional[Dict[str, str]] = None,
) -> DesignIndex:
    return DesignIndex.build(
        preprocessed,
        library_files=library_files,
        library_dirs=library_dirs,
        libexts=libexts,
        ignore_paths=ignore_paths,
        ignore_path_files=ignore_path_files,
        ignore_modules=ignore_modules,
        ignore_filelists=ignore_filelists,
        jobs=jobs,
        file_via_filelist=file_via_filelist,
        file_filelist_chain=file_filelist_chain,
    )


def flatten(
    modules: Dict[str, ModuleRecord],
    top: str,
    *,
    max_depth: Optional[int] = None,
) -> List[FlatRow]:
    index = DesignIndex(modules)
    return _flatten(index, top, max_depth=max_depth)


__all__ = [
    "DesignIndex",
    "ElabNode",
    "build_index",
    "elaborate",
    "flatten",
    "merge_library_stubs",
    "scan_all_preprocessed",
    "scan_preprocessed",
]