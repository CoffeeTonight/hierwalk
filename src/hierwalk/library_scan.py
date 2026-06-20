"""Scan -y/-v library paths for module stubs."""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Mapping, Sequence

from hierwalk.ignore_path import source_path_matches
from hierwalk.models import ModuleRecord

_MODULE_RE = re.compile(
    r"^\s*(?:module|interface|program)\s+([A-Za-z_]\w*)",
    re.MULTILINE | re.IGNORECASE,
)
_DEFAULT_EXTS = (".v", ".sv", ".vh", ".svh")


def _resolve_library_jobs(jobs: int, num_tasks: int) -> int:
    if jobs < 0:
        return 1
    if jobs == 0:
        cpu = os.cpu_count() or 1
        return max(1, min(cpu, num_tasks))
    return max(1, min(jobs, num_tasks))


def _stubs_from_text(path: Path, text: str) -> Dict[str, ModuleRecord]:
    stubs: Dict[str, ModuleRecord] = {}
    for m in _MODULE_RE.finditer(text):
        name = m.group(1)
        if name in stubs:
            continue
        stubs[name] = ModuleRecord(
            module_name=name,
            file_path=str(path.resolve()),
            is_blackbox=True,
        )
    return stubs


def _scan_library_file(path: Path) -> Dict[str, ModuleRecord]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    return _stubs_from_text(path, text)


def _collect_library_paths(
    library_files: Sequence[str | Path],
    library_dirs: Sequence[str | Path],
    *,
    libexts: Sequence[str],
    skip_path_patterns: Sequence[str],
) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    exts = tuple(libexts)

    def maybe_add(path: Path) -> None:
        if skip_path_patterns and source_path_matches(str(path), skip_path_patterns):
            return
        if not path.is_file():
            return
        if path.suffix and path.suffix not in exts:
            return
        key = str(path.resolve())
        if key in seen:
            return
        seen.add(key)
        paths.append(path)

    for lf in library_files:
        maybe_add(Path(lf))
    for ld in library_dirs:
        d = Path(ld)
        if not d.is_dir():
            continue
        for ext in exts:
            for path in d.rglob(f"*{ext}"):
                maybe_add(path)
    return paths


def scan_library_modules(
    library_files: Sequence[str | Path],
    library_dirs: Sequence[str | Path],
    *,
    libexts: Sequence[str] = _DEFAULT_EXTS,
    skip_path_patterns: Sequence[str] = (),
    jobs: int = 0,
) -> Dict[str, ModuleRecord]:
    paths = _collect_library_paths(
        library_files,
        library_dirs,
        libexts=libexts,
        skip_path_patterns=skip_path_patterns,
    )
    stubs: Dict[str, ModuleRecord] = {}
    if not paths:
        return stubs

    workers = _resolve_library_jobs(jobs, len(paths))

    def _merge_file_result(path: Path) -> None:
        for name, stub in _scan_library_file(path).items():
            if name not in stubs:
                stubs[name] = stub

    if workers == 1 or len(paths) <= 1:
        for path in paths:
            _merge_file_result(path)
        return stubs

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for per_file in pool.map(_scan_library_file, paths):
                for name, stub in per_file.items():
                    if name not in stubs:
                        stubs[name] = stub
    except (OSError, PermissionError, RuntimeError):
        for path in paths:
            _merge_file_result(path)
    return stubs