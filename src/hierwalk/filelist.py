"""hc_hierarchy-grade filelist expansion."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, TextIO, Tuple

from hierwalk.hch_compat.filelist_preprocess import FilelistResult as HchFilelistResult
from hierwalk.hch_compat.filelist_preprocess import expand_filelist
from hierwalk.models import FilelistLinkInfo


@dataclass
class FilelistResult:
    """Normalized view of :func:`expand_filelist` for hierwalk."""

    source_files: List[Path] = field(default_factory=list)
    include_dirs: List[Path] = field(default_factory=list)
    library_files: List[Path] = field(default_factory=list)
    library_dirs: List[Path] = field(default_factory=list)
    libexts: List[str] = field(default_factory=list)
    defines: Dict[str, str] = field(default_factory=dict)
    top_modules: List[str] = field(default_factory=list)
    slang_options: List[str] = field(default_factory=list)
    unsupported_options: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    index_cwd_used: Optional[Path] = None
    source_via_filelist: Dict[Path, Path] = field(default_factory=dict)
    source_filelist_chain: Dict[Path, str] = field(default_factory=dict)
    filelist_info: Dict[str, FilelistLinkInfo] = field(default_factory=dict)
    filelist_children: Dict[str, List[str]] = field(default_factory=dict)
    filelist_edges: List[Tuple[str, str, str]] = field(default_factory=list)
    raw: Optional[HchFilelistResult] = None


def _adapt(fl: HchFilelistResult) -> FilelistResult:
    info, children, edges = filelist_link_maps_from_raw(fl)
    return FilelistResult(
        source_files=list(fl.source_files),
        include_dirs=list(fl.incdirs),
        library_files=list(fl.library_files),
        library_dirs=list(fl.library_dirs),
        libexts=list(fl.libexts),
        defines=dict(fl.defines),
        top_modules=list(fl.top_modules),
        slang_options=list(fl.slang_options),
        unsupported_options=list(fl.unsupported_options),
        errors=list(fl.errors),
        index_cwd_used=fl.index_cwd_used,
        source_via_filelist=dict(fl.source_via_filelist),
        source_filelist_chain=dict(fl.source_filelist_chain),
        filelist_info=info,
        filelist_children=children,
        filelist_edges=edges,
        raw=fl,
    )


def filelist_link_maps_from_raw(
    fl: HchFilelistResult,
) -> tuple[Dict[str, FilelistLinkInfo], Dict[str, List[str]], List[Tuple[str, str, str]]]:
    info: Dict[str, FilelistLinkInfo] = {}
    for path, meta in fl.filelist_info.items():
        key = str(Path(path).resolve())
        info[key] = FilelistLinkInfo(
            path=key,
            exists=meta.get("exists") == "1",
            chain=meta.get("chain", ""),
            parent=meta.get("parent", ""),
            include_kind=meta.get("include_kind", ""),
        )
    children: Dict[str, List[str]] = {}
    for parent, kids in fl.filelist_children.items():
        pk = str(Path(parent).resolve())
        children[pk] = [str(Path(k).resolve()) for k in kids]
    edges = [
        (str(Path(a).resolve()), str(Path(b).resolve()), kind)
        for a, b, kind in fl.filelist_edges
    ]
    return info, children, edges


def filelist_provenance_maps(
    fl: FilelistResult,
) -> tuple[Dict[str, str], Dict[str, str]]:
    """Normalize RTL path -> listing filelist / chain."""
    from hierwalk.ignore_path import normalized_ignore_path

    via: Dict[str, str] = {}
    chain: Dict[str, str] = {}
    for src, listing in fl.source_via_filelist.items():
        key = normalized_ignore_path(src)
        via[key] = str(Path(listing).resolve())
    for src, path_chain in fl.source_filelist_chain.items():
        chain[normalized_ignore_path(src)] = path_chain
    return via, chain


def filelist_status_map(fl: FilelistResult) -> Dict[str, str]:
    """regexVerilogAST ``filelist[path] = 'True: chain'`` compatible view."""
    out: Dict[str, str] = {}
    for path, rec in fl.filelist_info.items():
        flag = "True" if rec.exists else "False"
        out[path] = f"{flag}: {rec.chain}"
    return out


def emit_filelist_failure(
    fl: FilelistResult,
    *,
    config_filelist: str,
    index_cwd: Optional[str] = None,
    stream: Optional[TextIO] = None,
) -> None:
    """Print which filelist / RTL paths failed (stderr)."""
    from hierwalk.hch_compat.platform_paths import (
        expand_path_vars,
        lookup_env_var,
        unexpanded_path_vars,
    )

    out = stream or sys.stderr
    raw_top = config_filelist
    if fl.raw is not None and fl.raw.raw_top_filelist:
        raw_top = fl.raw.raw_top_filelist
    resolved_top = fl.raw.top_path if fl.raw is not None else None

    print(f"run: filelist: FAIL — 0 RTL sources (config filelist={config_filelist!r})", file=out)
    if resolved_top is not None:
        print(f"run: filelist: resolved top .f path: {resolved_top}", file=out)
    if raw_top != config_filelist:
        print(f"run: filelist: raw top before JSON merge: {raw_top!r}", file=out)

    for label, raw in (("config", config_filelist), ("top", raw_top)):
        expanded = expand_path_vars(raw)
        if expanded != raw:
            print(f"run: filelist: env-expand ({label}) {raw!r} -> {expanded}", file=out)
        for var in unexpanded_path_vars(expanded):
            if lookup_env_var(var) is None:
                print(f"run: filelist: unset env var ${var} (used in {label} path)", file=out)

    if index_cwd:
        print(f"run: filelist: index-cwd (config)={index_cwd}", file=out)
    if fl.index_cwd_used:
        print(f"run: filelist: index-cwd (used for -F)={fl.index_cwd_used}", file=out)

    for err in fl.errors:
        print(f"run: filelist: {err}", file=out)

    for path, rec in fl.filelist_info.items():
        if not rec.exists:
            chain = rec.chain or path
            print(f"run: filelist: missing .f file: {path} (chain: {chain})", file=out)

    if not fl.errors and not any(not rec.exists for rec in fl.filelist_info.values()):
        print(
            "run: filelist: parsed .f file(s) contain no .v/.sv/.vh/.svh source lines",
            file=out,
        )


def parse_filelist(
    top_filelist: str,
    *,
    index_cwd: Optional[str] = None,
    extra_defines: Optional[Dict[str, str]] = None,
    env: Optional[Dict[str, str]] = None,
    on_progress: Optional[Callable[[str], None]] = None,
    ignore_filelists: Optional[Sequence[str]] = None,
    defer_source_exists: bool = False,
) -> FilelistResult:
    fl = expand_filelist(
        top_filelist,
        env,
        index_cwd=index_cwd,
        on_progress=on_progress,
        ignore_filelist_patterns=ignore_filelists,
        defer_source_exists=defer_source_exists,
    )
    if extra_defines:
        fl.defines.update(extra_defines)
    return _adapt(fl)