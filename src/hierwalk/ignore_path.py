"""Path patterns that stop hierarchy elaboration (ignorePath)."""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence

_MODULE_NAME_RE = re.compile(
    r"\b(?:module|interface|program)\s+([A-Za-z_]\w*)\b",
    re.IGNORECASE,
)


def _split_pattern_tokens(raw: str) -> List[str]:
    return [p.strip() for p in str(raw).split(",") if p.strip()]


def _append_unique(patterns: List[str], items: Iterable[str]) -> None:
    for item in items:
        token = item.strip()
        if token and token not in patterns:
            patterns.append(token)


def load_ignore_path_file(path: str | Path) -> tuple[List[str], List[str]]:
    """
    Load ignore patterns from a hand-edited list file.

    One pattern per line. ``#`` starts a comment. Inline commas are split.
    ``module:pcie_top`` lines add module-name ignores.
    ``filelist:pcie_block.f`` lines add listing-filelist ignores.
    """
    p = Path(path)
    paths: List[str] = []
    modules: List[str] = []
    filelists: List[str] = []
    if not p.is_file():
        return paths, modules, filelists
    text = p.read_text(encoding="utf-8", errors="ignore")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lower = line.lower()
        if lower.startswith("module:"):
            name = line.split(":", 1)[1].strip()
            _append_unique(modules, _split_pattern_tokens(name))
            continue
        if lower.startswith("filelist:"):
            name = line.split(":", 1)[1].strip()
            _append_unique(filelists, _split_pattern_tokens(name))
            continue
        _append_unique(paths, _split_pattern_tokens(line))
    return paths, modules, filelists


def load_ignore_lists(
    *,
    ignore_paths: Sequence[str] = (),
    ignore_path_files: Sequence[str | Path] = (),
    ignore_modules: Sequence[str] = (),
    ignore_filelists: Sequence[str] = (),
) -> tuple[List[str], List[str], List[str]]:
    """Merge CLI, env, files, and manual module / filelist names."""
    path_patterns: List[str] = []
    module_patterns: List[str] = []
    filelist_patterns: List[str] = []

    env = os.environ.get("HIERWALK_IGNORE_PATH", "").strip()
    if env:
        _append_unique(path_patterns, _split_pattern_tokens(env))

    env_mod = os.environ.get("HIERWALK_IGNORE_MODULE", "").strip()
    if env_mod:
        _append_unique(module_patterns, _split_pattern_tokens(env_mod))

    env_fl = os.environ.get("HIERWALK_IGNORE_FILELIST", "").strip()
    if env_fl:
        _append_unique(filelist_patterns, _split_pattern_tokens(env_fl))

    for p in ignore_paths:
        _append_unique(path_patterns, _split_pattern_tokens(p))

    for mf in ignore_path_files:
        file_paths, file_modules, file_filelists = load_ignore_path_file(mf)
        _append_unique(path_patterns, file_paths)
        _append_unique(module_patterns, file_modules)
        _append_unique(filelist_patterns, file_filelists)

    _append_unique(module_patterns, ignore_modules)
    _append_unique(filelist_patterns, ignore_filelists)
    return path_patterns, module_patterns, filelist_patterns


def resolve_ignore_path_patterns(
    ignore_paths: Sequence[str] = (),
    *,
    ignore_path_files: Sequence[str | Path] = (),
    ignore_modules: Sequence[str] = (),
    ignore_filelists: Sequence[str] = (),
) -> tuple[List[str], List[str], List[str]]:
    return load_ignore_lists(
        ignore_paths=ignore_paths,
        ignore_path_files=ignore_path_files,
        ignore_modules=ignore_modules,
        ignore_filelists=ignore_filelists,
    )


def _is_glob_pattern(pattern: str) -> bool:
    return any(ch in pattern for ch in ("*", "?", "["))


def _glob_matches_path(norm: str, pattern: str) -> bool:
    if fnmatch.fnmatchcase(norm, pattern):
        return True
    base = norm.rsplit("/", 1)[-1]
    if base and fnmatch.fnmatchcase(base, pattern):
        return True
    return any(fnmatch.fnmatchcase(seg, pattern) for seg in norm.split("/") if seg)


def _segment_matches(pattern: str, segment: str) -> bool:
    return pattern == segment or pattern.lower() == segment.lower()


def normalized_ignore_path(path: str | Path) -> str:
    """Canonical absolute path string for ignore-path matching."""
    try:
        return str(Path(path).resolve()).replace("\\", "/")
    except OSError:
        return str(path).replace("\\", "/")


def filelist_path_matches(
    listing_path: str | Path,
    *,
    chain: str = "",
    patterns: Sequence[str],
) -> bool:
    """Match listing ``.f`` path and/or provenance chain against ignore patterns."""
    if not patterns:
        return False
    listing_norm = normalized_ignore_path(listing_path) if listing_path else ""
    chain_norm = str(chain).replace("\\", "/")
    candidates: List[str] = []
    if listing_norm:
        candidates.append(listing_norm)
    if chain_norm:
        candidates.append(chain_norm)
    if not candidates:
        return False
    for pat in patterns:
        if not pat:
            continue
        for candidate in candidates:
            if _is_glob_pattern(pat):
                if _glob_matches_path(candidate, pat):
                    return True
            elif pat.lower() in candidate.lower():
                return True
            else:
                segments = [seg for seg in candidate.split("/") if seg]
                if any(_segment_matches(pat, seg) for seg in segments):
                    return True
        if listing_norm:
            base = Path(listing_norm).name
            if _is_glob_pattern(pat):
                if fnmatch.fnmatchcase(base, pat):
                    return True
            elif _segment_matches(pat, base):
                return True
    return False


def _lookup_provenance(
    resolved: str,
    mapping: Optional[Mapping[str, str]],
) -> str:
    if not mapping:
        return ""
    hit = mapping.get(resolved)
    if hit:
        return hit
    for key, value in mapping.items():
        if normalized_ignore_path(key) == resolved:
            return value
    return ""


def source_path_matches(path: str | Path, patterns: Sequence[str]) -> bool:
    if not patterns:
        return False
    norm = normalized_ignore_path(path)
    norm_lower = norm.lower()
    for pat in patterns:
        if not pat:
            continue
        if _is_glob_pattern(pat):
            if _glob_matches_path(norm, pat):
                return True
        elif pat.lower() in norm_lower:
            return True
        else:
            segments = [seg for seg in norm.split("/") if seg]
            if any(_segment_matches(pat, seg) for seg in segments):
                return True
    return False


def partition_sources(
    sources: Sequence[str],
    path_patterns: Sequence[str],
    *,
    filelist_patterns: Sequence[str] = (),
    file_via_filelist: Optional[Mapping[str, str]] = None,
    file_filelist_chain: Optional[Mapping[str, str]] = None,
) -> tuple[List[str], List[str]]:
    if not path_patterns and not filelist_patterns:
        return list(sources), []
    parse_out: List[str] = []
    ignore_out: List[str] = []
    for src in sources:
        resolved = normalized_ignore_path(src)
        if path_patterns and source_path_matches(resolved, path_patterns):
            ignore_out.append(resolved)
            continue
        if filelist_patterns:
            listing = _lookup_provenance(resolved, file_via_filelist)
            chain = _lookup_provenance(resolved, file_filelist_chain)
            if filelist_path_matches(
                listing,
                chain=chain,
                patterns=filelist_patterns,
            ):
                ignore_out.append(resolved)
                continue
        parse_out.append(resolved)
    return parse_out, ignore_out


def scan_ignore_path_stubs(text: str, file_path: str) -> dict[str, "ModuleRecord"]:
    from hierwalk.models import ModuleRecord

    out: dict[str, ModuleRecord] = {}
    for m in _MODULE_NAME_RE.finditer(text):
        name = m.group(1)
        if name in out:
            continue
        out[name] = ModuleRecord(
            module_name=name,
            file_path=file_path,
            stop_reason="ignorePath",
        )
    return out