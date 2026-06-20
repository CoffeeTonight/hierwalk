"""Hierarchy coverage audit: filelist RTL not reached from elaboration top(s)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

from hierwalk.filelist import FilelistResult
from hierwalk.index import DesignIndex
from hierwalk.models import FlatRow


def _norm_file(path: str | Path) -> str:
    return str(Path(path).resolve())


def minimal_unused_dir_roots(
    unused_files: Sequence[str],
    used_files: Sequence[str],
) -> List[str]:
    """
    Collapse untouched RTL paths to shallowest directory roots.

    If any file under a directory was elaborated, only report unused branches
    below the nearest elaborated ancestor (not the shared parent itself).
    """
    if not unused_files:
        return []
    used = {_norm_file(f) for f in used_files if f}
    if not used:
        return _collapse_dir_roots(str(Path(f).resolve().parent) for f in unused_files if f)

    roots: Set[str] = set()
    for raw in unused_files:
        if not raw:
            continue
        path = Path(raw).resolve()
        d = path.parent if path.is_file() else path
        chosen: Optional[Path] = None
        while True:
            if any(Path(u).is_relative_to(d) for u in used):
                break
            chosen = d
            if d.parent == d:
                break
            parent = d.parent
            if any(Path(u).is_relative_to(parent) for u in used):
                break
            d = parent
        if chosen is not None:
            roots.add(str(chosen))
    return _collapse_dir_roots(roots)


def _collapse_dir_roots(roots: Sequence[str]) -> List[str]:
    items = sorted({_norm_file(r) for r in roots if r})
    out: List[str] = []
    for r in items:
        if any(r != m and Path(r).is_relative_to(Path(m)) for m in out):
            continue
        out.append(r)
    return out


@dataclass(frozen=True)
class CoverageAuditResult:
    tops: tuple[str, ...] = ()
    listed_rtl: int = 0
    elaborated_rtl: int = 0
    untouched_rtl: int = 0
    unused_filelists: tuple[str, ...] = ()
    untouched_dir_roots: tuple[str, ...] = ()
    unused_filelist_count: int = 0
    untouched_dir_root_count: int = 0

    def summary_lines(self) -> List[str]:
        tops_note = ", ".join(self.tops) if self.tops else "(none)"
        out = [
            "Coverage audit (hierarchy not reached from top)",
            f"  Elab tops:           {tops_note}",
            f"  Listed RTL (filelist): {self.listed_rtl}",
            f"  Elab touched RTL:      {self.elaborated_rtl}",
            f"  Untouched RTL:         {self.untouched_rtl}",
            f"  Unused filelists:      {self.unused_filelist_count}",
            f"  Untouched dir roots:   {self.untouched_dir_root_count}",
        ]
        if self.unused_filelists:
            out.append("")
            out.append("Unused filelists (no listed RTL reached from top)")
            for fl_path in self.unused_filelists:
                out.append(f"  {fl_path}")
        if self.untouched_dir_roots:
            out.append("")
            out.append("Untouched directory roots (collapsed)")
            for d in self.untouched_dir_roots:
                out.append(f"  {d}")
        return out


def compute_coverage_audit(
    index: DesignIndex,
    fl: FilelistResult,
    rows: Sequence[FlatRow],
    *,
    tops: Sequence[str],
) -> CoverageAuditResult:
    """Compare filelist-listed RTL against elaboration rows from *tops*."""
    listed = {_norm_file(p) for p in fl.source_files}
    reachable = {_norm_file(r.file) for r in rows if r.file}
    reachable &= listed
    untouched = listed - reachable

    by_listing: Dict[str, Set[str]] = defaultdict(set)
    for src, listing in index.file_via_filelist.items():
        key = _norm_file(src)
        if key in listed and listing:
            by_listing[_norm_file(listing)].add(key)

    unused_filelists: List[str] = []
    for fl_path in sorted(index.filelist_info.keys(), key=str):
        norm_fl = _norm_file(fl_path)
        sources = by_listing.get(norm_fl, set())
        if not sources:
            unused_filelists.append(norm_fl)
        elif not (sources & reachable):
            unused_filelists.append(norm_fl)

    dir_roots = minimal_unused_dir_roots(sorted(untouched), sorted(reachable))

    return CoverageAuditResult(
        tops=tuple(tops),
        listed_rtl=len(listed),
        elaborated_rtl=len(reachable),
        untouched_rtl=len(untouched),
        unused_filelists=tuple(unused_filelists),
        untouched_dir_roots=tuple(dir_roots),
        unused_filelist_count=len(unused_filelists),
        untouched_dir_root_count=len(dir_roots),
    )