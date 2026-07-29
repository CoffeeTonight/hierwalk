"""Compile context: filelist expansion + stable context_id for caches/DB."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Union

from pyhirewalk.filelist.expand import (
    FilelistResult,
    build_slang_filelist_lines,
    expand_filelist,
    write_slang_filelist,
)
from pyhirewalk.filelist.paths import path_to_posix

OnProgress = Callable[[str], None]


@dataclass(frozen=True)
class CompileContext:
    """
    One reproducible compile configuration.

    All downstream artifacts (index, hierarchy skeleton, COI slice KG) must
    key off :attr:`context_id` so define / filelist changes never mix.
    """

    context_id: str
    top_filelist: Path
    index_cwd: Path
    source_files: tuple[Path, ...]
    incdirs: tuple[Path, ...]
    defines: Dict[str, str]
    top_modules: tuple[str, ...]
    library_files: tuple[Path, ...]
    library_dirs: tuple[Path, ...]
    libexts: tuple[str, ...]
    slang_options: tuple[str, ...]
    unsupported_options: tuple[str, ...]
    errors: tuple[str, ...]
    # Provenance kept as posix strings for JSON/DB friendliness
    source_via_filelist: Dict[str, str] = field(default_factory=dict)
    source_filelist_chain: Dict[str, str] = field(default_factory=dict)
    filelist_edges: tuple[tuple[str, str, str], ...] = ()
    extra_defines: Dict[str, str] = field(default_factory=dict)
    raw: Optional[FilelistResult] = field(default=None, repr=False, compare=False)

    def slang_lines(self) -> List[str]:
        if self.raw is not None:
            return build_slang_filelist_lines(self.raw)
        # Rebuild a minimal FilelistResult-shaped flatten without re-expand
        lines: List[str] = []
        if self.libexts:
            lines.append("+libext+" + "+".join(self.libexts))
        for inc in self.incdirs:
            lines.append(f"+incdir+{path_to_posix(inc)}")
        merged = {**self.defines, **self.extra_defines}
        for name, val in sorted(merged.items()):
            lines.append(f"+define+{name}={val}" if val else f"+define+{name}")
        for ydir in self.library_dirs:
            lines.append(f"-y {path_to_posix(ydir)}")
        for vfile in self.library_files:
            lines.append(f"-v {path_to_posix(vfile)}")
        lines.extend(self.slang_options)
        for src in self.source_files:
            lines.append(path_to_posix(src))
        return lines

    def write_slang_filelist(self, dest: Union[str, Path]) -> Path:
        if self.raw is not None:
            # Ensure extra_defines are reflected
            if self.extra_defines:
                fl = self.raw
                fl.defines.update(self.extra_defines)
            return write_slang_filelist(self.raw, dest)
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(self.slang_lines()) + "\n", encoding="utf-8")
        return out.resolve()

    def summary(self) -> Dict[str, object]:
        return {
            "context_id": self.context_id,
            "top_filelist": path_to_posix(self.top_filelist),
            "index_cwd": path_to_posix(self.index_cwd),
            "n_sources": len(self.source_files),
            "n_incdirs": len(self.incdirs),
            "n_defines": len(self.defines) + len(self.extra_defines),
            "top_modules": list(self.top_modules),
            "n_errors": len(self.errors),
            "n_filelist_edges": len(self.filelist_edges),
        }


def _canonical_context_payload(
    *,
    top_filelist: Path,
    index_cwd: Path,
    sources: Sequence[Path],
    incdirs: Sequence[Path],
    defines: Mapping[str, str],
    top_modules: Sequence[str],
    library_files: Sequence[Path],
    library_dirs: Sequence[Path],
    slang_options: Sequence[str],
) -> str:
    payload = {
        "top_filelist": path_to_posix(top_filelist),
        "index_cwd": path_to_posix(index_cwd),
        "sources": sorted(path_to_posix(p) for p in sources),
        "incdirs": sorted(path_to_posix(p) for p in incdirs),
        "defines": dict(sorted(defines.items())),
        "top_modules": list(top_modules),
        "library_files": sorted(path_to_posix(p) for p in library_files),
        "library_dirs": sorted(path_to_posix(p) for p in library_dirs),
        "slang_options": list(slang_options),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def context_id_from_parts(
    *,
    top_filelist: Path,
    index_cwd: Path,
    sources: Sequence[Path],
    incdirs: Sequence[Path],
    defines: Mapping[str, str],
    top_modules: Sequence[str] = (),
    library_files: Sequence[Path] = (),
    library_dirs: Sequence[Path] = (),
    slang_options: Sequence[str] = (),
) -> str:
    blob = _canonical_context_payload(
        top_filelist=top_filelist,
        index_cwd=index_cwd,
        sources=sources,
        incdirs=incdirs,
        defines=defines,
        top_modules=top_modules,
        library_files=library_files,
        library_dirs=library_dirs,
        slang_options=slang_options,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def build_context(
    top_filelist: Union[str, Path],
    *,
    index_cwd: Optional[Union[str, Path]] = None,
    extra_defines: Optional[Mapping[str, str]] = None,
    env: Optional[Mapping[str, str]] = None,
    top: Optional[str] = None,
    ignore_filelist_patterns: Optional[Sequence[str]] = None,
    defer_source_exists: bool = False,
    on_progress: Optional[OnProgress] = None,
) -> CompileContext:
    """
    Expand ``top_filelist`` and build a :class:`CompileContext`.

    ``extra_defines`` are merged on top of filelist ``+define+`` (CLI wins).
    Filelist progress logs only the parent (top) ``.f``, not nested lists.
    """
    fl = expand_filelist(
        top_filelist,
        env,
        index_cwd=index_cwd,
        on_progress=on_progress,
        ignore_filelist_patterns=ignore_filelist_patterns,
        defer_source_exists=defer_source_exists,
    )
    extra = dict(extra_defines or {})
    merged_defines = {**fl.defines, **extra}
    tops = list(fl.top_modules)
    if top and top not in tops:
        tops.insert(0, top)

    cwd = fl.index_cwd_used or fl.base_dir
    cid = context_id_from_parts(
        top_filelist=fl.top_path,
        index_cwd=cwd,
        sources=fl.source_files,
        incdirs=fl.incdirs,
        defines=merged_defines,
        top_modules=tops,
        library_files=fl.library_files,
        library_dirs=fl.library_dirs,
        slang_options=fl.slang_options,
    )

    via = {path_to_posix(k): path_to_posix(v) for k, v in fl.source_via_filelist.items()}
    chain = {
        path_to_posix(k): v for k, v in fl.source_filelist_chain.items()
    }
    edges = tuple(
        (path_to_posix(a), path_to_posix(b), kind) for a, b, kind in fl.filelist_edges
    )

    if extra:
        fl.defines.update(extra)

    return CompileContext(
        context_id=cid,
        top_filelist=fl.top_path,
        index_cwd=cwd,
        source_files=tuple(fl.source_files),
        incdirs=tuple(fl.incdirs),
        defines=dict(fl.defines),
        top_modules=tuple(tops),
        library_files=tuple(fl.library_files),
        library_dirs=tuple(fl.library_dirs),
        libexts=tuple(fl.libexts),
        slang_options=tuple(fl.slang_options),
        unsupported_options=tuple(fl.unsupported_options),
        errors=tuple(fl.errors),
        source_via_filelist=via,
        source_filelist_chain=chain,
        filelist_edges=edges,
        extra_defines=extra,
        raw=fl,
    )
