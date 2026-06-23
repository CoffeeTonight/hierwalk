"""Disk cache for DesignIndex and per-top elaboration results."""

from __future__ import annotations

import hashlib
import os
import pickle
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from hierwalk.filelist import FilelistResult, filelist_provenance_maps
from hierwalk.index import DesignIndex
from hierwalk.manifest import (
    SourceManifest,
    build_source_manifest,
    collect_index_digest_paths,
    config_cache_key,
    digest_scope,
    hash_paths_parallel,
    manifest_diff,
    manifest_is_current,
)
from hierwalk.models import ElabNode, FlatRow

CACHE_VERSION = 8

_active_work_dir: Optional[Path] = None


def set_active_work_dir(path: Optional[Path]) -> None:
    global _active_work_dir
    _active_work_dir = path.resolve() if path is not None else None


def get_active_work_dir() -> Optional[Path]:
    return _active_work_dir


@dataclass
class ScanInstCacheBundle:
    version: int
    config_key: str
    source_manifest: SourceManifest
    index: DesignIndex
    elab: Dict[str, Tuple[ElabNode, List[FlatRow]]] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return self.config_key


_TOP_SAFE_RE = re.compile(r"[^\w.-]+")


def sanitize_top_name(top: str) -> str:
    safe = _TOP_SAFE_RE.sub("_", top.strip())
    return safe.strip("._") or "top"


def top_work_dir_name(top: str) -> str:
    return f".db_{sanitize_top_name(top)}"


def work_base_dir() -> Path:
    """Shell cwd for ``.db_{TOP}/`` (``index-cwd`` is filelist-only, not work-dir)."""
    return Path.cwd()


def top_work_dir(top: str, *, base: Optional[Path] = None) -> Path:
    return (base or Path.cwd()) / top_work_dir_name(top)


def ensure_top_work_dir(top: str, *, base: Optional[Path] = None) -> Path:
    root = top_work_dir(top, base=base)
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    return root


def resolve_top_label(
    *,
    cfg_top: str = "",
    connect_top: str = "",
    inst_trace_top: str = "",
    filelist_tops: Sequence[str] = (),
    filelist_path: str = "",
) -> str:
    for candidate in (cfg_top, connect_top, inst_trace_top):
        if candidate and candidate.strip():
            return candidate.strip()
    if filelist_tops:
        return str(filelist_tops[0])
    if filelist_path:
        return Path(filelist_path).stem
    return "top"


def resolve_run_work_dir(
    top: str,
    *,
    base: Optional[Path] = None,
    explicit_cache_dir: Optional[str] = None,
) -> Path:
    """Per-run work root: always ``.db_{TOP}`` under *base* (or under ``HIERWALK_CACHE_DIR``)."""
    resolved_base = base
    if explicit_cache_dir:
        path = Path(explicit_cache_dir).expanduser().resolve()
        if path.name.startswith(".db_"):
            path.mkdir(parents=True, exist_ok=True)
            return path
        resolved_base = path
    elif os.environ.get("HIERWALK_CACHE_DIR"):
        resolved_base = Path(os.environ["HIERWALK_CACHE_DIR"]).expanduser().resolve()
    if resolved_base is None:
        resolved_base = work_base_dir()
    return ensure_top_work_dir(top, base=resolved_base)


def default_cache_dir() -> Path:
    """Legacy fallback when top / index-cwd are unknown (prefer :func:`resolve_run_work_dir`)."""
    env = os.environ.get("HIERWALK_CACHE_DIR")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "hier-walk"
    return Path.home() / ".cache" / "hier-walk"


def cache_path_for(cache_dir: Path, config_key: str) -> Path:
    return cache_dir / f"{config_key}.hier-walk.pkl"


def elab_cache_dir(cache_dir: Path, config_key: str) -> Path:
    return cache_dir / config_key / "elab"


def _elab_sidecar_path(cache_dir: Path, config_key: str, elab_key: str) -> Path:
    digest = hashlib.sha256(elab_key.encode("utf-8")).hexdigest()[:16]
    return elab_cache_dir(cache_dir, config_key) / f"{digest}.elab.pkl"


def elab_cache_key(top: str, max_depth: Optional[int]) -> str:
    depth = -1 if max_depth is None else max_depth
    return f"{top}\x00{depth}"


def _load_elab_sidecars(
    cache_dir: Path,
    config_key: str,
) -> Dict[str, Tuple[ElabNode, List[FlatRow]]]:
    out: Dict[str, Tuple[ElabNode, List[FlatRow]]] = {}
    edir = elab_cache_dir(cache_dir, config_key)
    if not edir.is_dir():
        return out
    for path in edir.glob("*.elab.pkl"):
        try:
            with path.open("rb") as fh:
                payload = pickle.load(fh)
        except (OSError, pickle.PickleError, EOFError, ValueError):
            continue
        if (
            isinstance(payload, tuple)
            and len(payload) == 2
            and isinstance(payload[0], str)
            and isinstance(payload[1], tuple)
            and len(payload[1]) == 2
        ):
            key, entry = payload
            out[key] = entry
    return out


def _save_elab_sidecar(
    cache_dir: Path,
    config_key: str,
    elab_key: str,
    entry: Tuple[ElabNode, List[FlatRow]],
) -> None:
    path = _elab_sidecar_path(cache_dir, config_key, elab_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as fh:
        pickle.dump((elab_key, entry), fh, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)


def load_cache(path: Path, *, cache_dir: Optional[Path] = None) -> Optional[ScanInstCacheBundle]:
    if not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            obj = pickle.load(fh)
    except (OSError, pickle.PickleError, EOFError, ValueError):
        return None
    if not isinstance(obj, ScanInstCacheBundle):
        return None
    if obj.version != CACHE_VERSION:
        return None
    base = cache_dir if cache_dir is not None else path.parent
    obj.elab = _load_elab_sidecars(base, obj.config_key)
    return obj


def save_cache(path: Path, bundle: ScanInstCacheBundle) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    slim = ScanInstCacheBundle(
        version=bundle.version,
        config_key=bundle.config_key,
        source_manifest=bundle.source_manifest,
        index=bundle.index,
        elab={},
    )
    with tmp.open("wb") as fh:
        pickle.dump(slim, fh, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)


def build_design_index(
    fl: FilelistResult,
    *,
    ignore_paths: Sequence[str],
    ignore_path_files: Sequence[str],
    ignore_modules: Sequence[str],
    ignore_filelists: Sequence[str],
    jobs: int,
    low_memory: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
    source_subset: Optional[Sequence[str]] = None,
) -> DesignIndex:
    sources = (
        [str(p) for p in fl.source_files]
        if source_subset is None
        else list(source_subset)
    )
    via_map, chain_map = filelist_provenance_maps(fl)
    jobs_note = "auto" if jobs == 0 else str(jobs)
    if on_progress:
        on_progress(
            f"index: building from {len(sources)} sources "
            f"({len(fl.filelist_info)} filelists, jobs={jobs_note})"
        )
    return DesignIndex.build_from_sources(
        sources,
        include_dirs=[str(p) for p in fl.include_dirs],
        defines=fl.defines,
        library_files=[str(p) for p in fl.library_files],
        library_dirs=[str(p) for p in fl.library_dirs],
        libexts=list(fl.libexts),
        ignore_paths=list(ignore_paths),
        ignore_path_files=list(ignore_path_files),
        ignore_modules=list(ignore_modules),
        ignore_filelists=list(ignore_filelists),
        jobs=jobs,
        low_memory=low_memory,
        on_progress=on_progress,
        file_via_filelist=via_map,
        file_filelist_chain=chain_map,
        filelist_info=fl.filelist_info,
        filelist_children=fl.filelist_children,
        filelist_edges=fl.filelist_edges,
    )


def _incremental_update(
    bundle: ScanInstCacheBundle,
    fl: FilelistResult,
    manifest: SourceManifest,
    *,
    changed: set[str],
    removed: set[str],
    added: set[str],
    ignore_paths: Sequence[str],
    ignore_path_files: Sequence[str],
    ignore_modules: Sequence[str],
    ignore_filelists: Sequence[str],
    jobs: int,
    on_progress: Optional[Callable[[str], None]] = None,
) -> DesignIndex:
    touch = sorted(changed | added)
    if on_progress:
        on_progress(
            f"cache: incremental update ({len(touch)} changed/new, "
            f"{len(removed)} removed)"
        )
    bundle.index.patch_files(
        touch,
        sorted(removed),
        include_dirs=[str(p) for p in fl.include_dirs],
        defines=fl.defines,
        jobs=jobs,
        on_progress=on_progress,
    )
    bundle.source_manifest = dict(manifest)
    return bundle.index


def load_or_build_index(
    filelist_path: str | Path,
    fl: FilelistResult,
    *,
    cache_dir: Path,
    extra_defines: Mapping[str, str],
    ignore_paths: Sequence[str],
    ignore_path_files: Sequence[str],
    ignore_modules: Sequence[str],
    ignore_filelists: Sequence[str],
    jobs: int,
    use_cache: bool,
    refresh_cache: bool,
    low_memory: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
) -> tuple[DesignIndex, ScanInstCacheBundle, bool, bool, bool, Path]:
    """
    Return (index, bundle, index_cache_hit, rebuilt_index, incremental, cache_path).

    ``index_cache_hit`` is True on a full manifest match (no RTL rescan).
    ``incremental`` is True when only changed sources were rescanned.
    """
    digest_paths = collect_index_digest_paths(
        filelist_path,
        fl,
        ignore_path_files=ignore_path_files,
    )
    if on_progress:
        on_progress(f"cache: hashing {len(digest_paths)} inputs")
    path_digests = hash_paths_parallel(digest_paths, jobs=jobs)

    with digest_scope(path_digests):
        config_key = config_cache_key(
            filelist_path,
            fl,
            cache_version=CACHE_VERSION,
            extra_defines=extra_defines,
            ignore_paths=ignore_paths,
            ignore_path_files=ignore_path_files,
            ignore_modules=ignore_modules,
            ignore_filelists=ignore_filelists,
            path_digests=path_digests,
        )
        path = cache_path_for(cache_dir, config_key)
        bundle: Optional[ScanInstCacheBundle] = None
        index_cache_hit = False
        rebuilt_index = True
        incremental = False

        if use_cache and not refresh_cache:
            if on_progress:
                on_progress("cache: checking index cache")
            bundle = load_cache(path, cache_dir=cache_dir)

        manifest = build_source_manifest(
            fl,
            ignore_path_files=ignore_path_files,
            path_digests=path_digests,
        )

        if use_cache and not refresh_cache and bundle is not None:
            if bundle.config_key == config_key:
                if manifest_is_current(bundle.source_manifest, manifest):
                    index_cache_hit = True
                    rebuilt_index = False
                    if on_progress:
                        on_progress(
                            f"cache: loaded index ({len(bundle.index.modules)} modules)"
                        )
                    return bundle.index, bundle, True, False, False, path
                changed, removed, added = manifest_diff(
                    bundle.source_manifest,
                    manifest,
                )
                if changed or removed or added:
                    index = _incremental_update(
                        bundle,
                        fl,
                        manifest,
                        changed=changed,
                        removed=removed,
                        added=added,
                        ignore_paths=ignore_paths,
                        ignore_path_files=ignore_path_files,
                        ignore_modules=ignore_modules,
                        ignore_filelists=ignore_filelists,
                        jobs=jobs,
                        on_progress=on_progress,
                    )
                    save_cache(path, bundle)
                    if on_progress:
                        on_progress(
                            f"cache: incremental save ({len(index.modules)} modules)"
                        )
                    incremental = True
                    rebuilt_index = True
                    return index, bundle, False, rebuilt_index, incremental, path
            if on_progress and path.is_file():
                on_progress("cache: stale or unreadable, rebuilding index")

        if on_progress:
            on_progress("index: building (no cache hit)")
        index = build_design_index(
            fl,
            ignore_paths=ignore_paths,
            ignore_path_files=ignore_path_files,
            ignore_modules=ignore_modules,
            ignore_filelists=ignore_filelists,
            jobs=jobs,
            low_memory=low_memory,
            on_progress=on_progress,
        )
        bundle = ScanInstCacheBundle(
            version=CACHE_VERSION,
            config_key=config_key,
            source_manifest=dict(manifest),
            index=index,
            elab=bundle.elab if bundle is not None else {},
        )
        if use_cache:
            if on_progress:
                on_progress(f"cache: saving index ({len(index.modules)} modules)")
            save_cache(path, bundle)
        elif on_progress:
            on_progress(f"index: done ({len(index.modules)} modules)")
        return index, bundle, index_cache_hit, rebuilt_index, incremental, path


def get_cached_elab(
    bundle: ScanInstCacheBundle,
    top: str,
    max_depth: Optional[int],
) -> Optional[Tuple[ElabNode, List[FlatRow]]]:
    return bundle.elab.get(elab_cache_key(top, max_depth))


def store_cached_elab(
    bundle: ScanInstCacheBundle,
    top: str,
    max_depth: Optional[int],
    root: ElabNode,
    rows: List[FlatRow],
    *,
    cache_dir: Path,
    use_cache: bool,
) -> bool:
    key = elab_cache_key(top, max_depth)
    bundle.elab[key] = (root, rows)
    if not use_cache:
        return False
    _save_elab_sidecar(cache_dir, bundle.config_key, key, (root, rows))
    return True