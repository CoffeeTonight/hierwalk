"""Source manifest for cache validation and incremental index."""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, Mapping, Optional, Sequence, Union

from hierwalk.filelist import FilelistResult

SourceStat = str  # sha256 hex digest of file bytes
SourceManifest = Dict[str, SourceStat]
PathDigests = Dict[str, str]

_CHUNK_BYTES = 1024 * 1024
_PARALLEL_MIN_FILES = 8

_active_digests: Optional[PathDigests] = None


def _manifest_digest(value: Union[str, object]) -> str:
    digest = getattr(value, "digest", None)
    if isinstance(digest, str):
        return digest
    if isinstance(value, str):
        return value
    raise TypeError(f"unsupported manifest entry: {type(value)!r}")


def _read_file_digest(path: Path) -> Optional[str]:
    try:
        hasher = hashlib.sha256()
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(_CHUNK_BYTES)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError:
        return None


def _lookup_digest(
    path: Path,
    path_digests: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    key = str(path.resolve())
    if path_digests is not None:
        hit = path_digests.get(key)
        if hit is not None:
            return hit
    if _active_digests is not None:
        hit = _active_digests.get(key)
        if hit is not None:
            return hit
    return None


def path_content_digest(
    path: Path,
    *,
    path_digests: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Hash file bytes. Use precomputed ``path_digests`` / active scope when provided."""
    hit = _lookup_digest(path, path_digests)
    if hit is not None:
        return hit
    return _read_file_digest(path)


@contextmanager
def digest_scope(path_digests: Mapping[str, str]) -> Iterator[None]:
    """Reuse digests computed once per hier-walk / path-walk operation."""
    global _active_digests
    prev = _active_digests
    _active_digests = dict(path_digests)
    try:
        yield
    finally:
        _active_digests = prev


def set_digest_scope(path_digests: Mapping[str, str]) -> None:
    global _active_digests
    _active_digests = dict(path_digests)


def clear_digest_scope() -> None:
    global _active_digests
    _active_digests = None


def _resolve_manifest_jobs(jobs: int, num_tasks: int) -> int:
    if jobs < 0:
        return 1
    if jobs == 0:
        cpu = os.cpu_count() or 1
        return max(1, min(cpu, num_tasks))
    return max(1, min(jobs, num_tasks))


def hash_paths_parallel(
    paths: Sequence[str | Path],
    *,
    jobs: int = 0,
) -> PathDigests:
    """One parallel pass over unique paths; always reads bytes (no mtime fast-path)."""
    unique = sorted({str(Path(raw).resolve()) for raw in paths})
    out: PathDigests = {}
    if not unique:
        return out

    def work(path_str: str) -> tuple[str, Optional[str]]:
        return path_str, _read_file_digest(Path(path_str))

    workers = _resolve_manifest_jobs(jobs, len(unique))
    if workers <= 1 or len(unique) < _PARALLEL_MIN_FILES:
        for path_str in unique:
            key, digest = work(path_str)
            if digest is not None:
                out[key] = digest
        return out

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, path_str) for path_str in unique]
        for fut in as_completed(futures):
            key, digest = fut.result()
            if digest is not None:
                out[key] = digest
    return out


def _collect_manifest_paths(
    fl: FilelistResult,
    ignore_path_files: Sequence[str],
) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []

    def add(raw: str | Path) -> None:
        key = str(Path(raw).resolve())
        if key not in seen:
            seen.add(key)
            ordered.append(key)

    for src in fl.source_files:
        add(src)
    for lib in fl.library_files:
        add(lib)
    for fl_path in fl.filelist_info:
        add(fl_path)
    for raw in ignore_path_files:
        add(raw)
    return ordered


def collect_index_digest_paths(
    filelist_path: str | Path,
    fl: FilelistResult,
    *,
    ignore_path_files: Sequence[str] = (),
) -> list[str]:
    """Paths hashed once for full-index cache key + source manifest."""
    paths = set(_collect_manifest_paths(fl, ignore_path_files))
    paths.add(str(Path(filelist_path).resolve()))
    return sorted(paths)


def build_source_manifest(
    fl: FilelistResult,
    *,
    ignore_path_files: Sequence[str] = (),
    jobs: int = 0,
    path_digests: Optional[Mapping[str, str]] = None,
) -> SourceManifest:
    paths = _collect_manifest_paths(fl, ignore_path_files)
    if path_digests is None:
        path_digests = hash_paths_parallel(paths, jobs=jobs)

    manifest: SourceManifest = {}
    for path_str in paths:
        digest = path_content_digest(Path(path_str), path_digests=path_digests)
        if digest is not None:
            manifest[path_str] = digest
    return manifest


def manifest_diff(
    old: Mapping[str, SourceStat],
    new: SourceManifest,
) -> tuple[set[str], set[str], set[str]]:
    old_keys = set(old)
    new_keys = set(new)
    added = new_keys - old_keys
    removed = old_keys - new_keys
    changed = {
        key
        for key in old_keys & new_keys
        if _manifest_digest(old[key]) != new[key]
    }
    return changed, removed, added


def manifest_is_current(
    old: Mapping[str, SourceStat],
    new: SourceManifest,
) -> bool:
    if set(old) != set(new):
        return False
    return all(_manifest_digest(old[key]) == new[key] for key in new)


def _feed(hasher: "hashlib._Hash", text: str) -> None:
    hasher.update(text.encode("utf-8"))
    hasher.update(b"\0")


def _feed_mapping(hasher: "hashlib._Hash", data: Mapping[str, str]) -> None:
    for key in sorted(data):
        _feed(hasher, key)
        _feed(hasher, str(data[key]))


def _feed_patterns(hasher: "hashlib._Hash", patterns: Sequence[str]) -> None:
    for pat in sorted(set(patterns)):
        _feed(hasher, pat)


def config_cache_key(
    filelist_path: str | Path,
    fl: FilelistResult,
    *,
    cache_version: int,
    extra_defines: Mapping[str, str],
    ignore_paths: Sequence[str],
    ignore_path_files: Sequence[str],
    ignore_modules: Sequence[str],
    ignore_filelists: Sequence[str],
    path_digests: Optional[Mapping[str, str]] = None,
) -> str:
    """Stable cache filename key (no per-source stats)."""
    hasher = hashlib.sha256()
    _feed(hasher, f"version={cache_version}")
    p = Path(filelist_path)
    p_key = str(p.resolve())
    _feed(hasher, p_key)
    digest = path_content_digest(p, path_digests=path_digests)
    if digest is not None:
        hasher.update(digest.encode())
    else:
        _feed(hasher, "missing")
    if fl.index_cwd_used:
        _feed(hasher, f"index_cwd={fl.index_cwd_used.resolve()}")
    defines = dict(fl.defines)
    for key, val in extra_defines.items():
        defines[key] = val
    _feed_mapping(hasher, defines)
    _feed_patterns(hasher, ignore_paths)
    _feed_patterns(hasher, ignore_modules)
    _feed_patterns(hasher, ignore_filelists)
    for ignore_file in sorted(ignore_path_files):
        ip = Path(ignore_file)
        ip_key = str(ip.resolve())
        _feed(hasher, ip_key)
        ip_digest = path_content_digest(ip, path_digests=path_digests)
        if ip_digest is not None:
            hasher.update(ip_digest.encode())
    for inc in sorted(fl.include_dirs, key=lambda x: str(x)):
        _feed(hasher, str(inc.resolve()))
    for libdir in sorted(fl.library_dirs, key=lambda x: str(x)):
        _feed(hasher, str(libdir.resolve()))
    for ext in fl.libexts:
        _feed(hasher, ext)
    return hasher.hexdigest()


def scan_chunksize(num_tasks: int, workers: int) -> int:
    if num_tasks <= 64:
        return 1
    return max(1, min(64, num_tasks // max(workers * 4, 1)))