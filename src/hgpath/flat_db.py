"""Flat DB: module → filepath (wraps hierwalk grep_hie)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from hierwalk.connect.hierarchy_grep_gate import prepare_hierarchy_grep_session as _legacy_prepare
from hierwalk.hierarchy_grep import (
    HierarchyGrepSession,
    dump_grep_hie,
    grep_hie_filelist_match,
    grep_hie_sources_match,
    load_grep_hie,
    normalize_rtl_paths,
    remove_grep_hie,
    resolve_grep_hie_path,
)

FLAT_JSON_NAME = "hgpath_flat.json"


def resolve_flat_db_path(work_dir: str | Path) -> Path:
    return Path(work_dir).expanduser().resolve() / FLAT_JSON_NAME


@dataclass
class FlatDb:
    rtl_paths: List[str]
    module_index: Dict[str, List[str]]
    work_dir: Path
    path: Path

    @property
    def module_count(self) -> int:
        return len(self.module_index)

    @property
    def rtl_file_count(self) -> int:
        return len(self.rtl_paths)


def _link_legacy_grep_hie_cache(*, flat_path: Path, legacy_path: Path) -> None:
    """Keep ``grep_hie.json`` alias for hier-walk tools sharing the work dir."""
    if legacy_path == flat_path:
        return
    if legacy_path.is_file() or legacy_path.is_symlink():
        legacy_path.unlink()
    try:
        legacy_path.symlink_to(flat_path.name)
    except OSError:
        shutil.copy2(flat_path, legacy_path)


def _upgrade_grep_hie_fingerprint(
    cached: dict,
    session: HierarchyGrepSession,
    *,
    flat_path: Path,
    top: str,
    filelist: str,
    index_cwd: Optional[str],
) -> None:
    """Add filelist fingerprint to legacy caches so warm runs can skip expand."""
    if not filelist or cached.get("filelist"):
        return
    dump_grep_hie(
        session,
        flat_path,
        top=top or str(cached.get("top") or ""),
        filelist=filelist,
        index_cwd=index_cwd,
    )


def _flat_db_from_session(
    session: HierarchyGrepSession,
    *,
    work: Path,
    flat_path: Path,
) -> FlatDb:
    return FlatDb(
        rtl_paths=list(session.rtl_paths),
        module_index=dict(session.module_index),
        work_dir=work,
        path=flat_path,
    )


def try_load_flat_db_cache(
    *,
    work_dir: str | Path,
    filelist: str,
    index_cwd: Optional[str] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> Optional[tuple[FlatDb, HierarchyGrepSession]]:
    """
    Warm-path load when flat DB exists and top filelist fingerprint matches.

    Skips nested filelist expansion; caller should use
    :func:`hierwalk.filelist.filelist_result_from_grep_hie` for a FilelistResult.
    """
    work = Path(work_dir).expanduser().resolve()
    flat_path = resolve_flat_db_path(work)
    if not flat_path.is_file() or not filelist:
        return None

    def _log(msg: str) -> None:
        if on_log is not None:
            on_log(msg)

    try:
        cached = load_grep_hie(flat_path)
        if not grep_hie_filelist_match(cached, filelist, index_cwd=index_cwd):
            return None
        session = HierarchyGrepSession.from_grep_hie_cache(cached, cache_path=flat_path)
        _log(
            f"flat-db hit skip-filelist-expand path={flat_path} "
            f"modules={len(session.module_index)} rtl_files={len(session.rtl_paths)}"
        )
        return _flat_db_from_session(session, work=work, flat_path=flat_path), session
    except (OSError, ValueError):
        return None


def load_or_build_flat_db(
    sources: Sequence[str],
    *,
    top: str,
    work_dir: str | Path,
    refresh: bool = False,
    filelist: str = "",
    index_cwd: Optional[str] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> tuple[FlatDb, HierarchyGrepSession]:
    """Load ``hgpath_flat.json`` or build from RTL (legacy grep_hie session)."""
    work = Path(work_dir).expanduser().resolve()
    flat_path = resolve_flat_db_path(work)
    legacy_path = resolve_grep_hie_path(work)

    def _log(msg: str) -> None:
        if on_log is not None:
            on_log(msg)

    if refresh and flat_path.is_file():
        flat_path.unlink()
    if refresh and legacy_path.is_file():
        remove_grep_hie(legacy_path)

    paths = normalize_rtl_paths(sources, already_normalized=False)

    if flat_path.is_file() and not refresh:
        try:
            cached = load_grep_hie(flat_path)
            fingerprint_ok = (
                bool(filelist)
                and grep_hie_filelist_match(cached, filelist, index_cwd=index_cwd)
            )
            if fingerprint_ok or grep_hie_sources_match(cached, paths):
                _log(f"flat-db hit path={flat_path} modules={len(cached.get('module_index', {}))}")
                session = HierarchyGrepSession.from_grep_hie_cache(cached, cache_path=flat_path)
                _upgrade_grep_hie_fingerprint(
                    cached,
                    session,
                    flat_path=flat_path,
                    top=top,
                    filelist=filelist,
                    index_cwd=index_cwd,
                )
                return _flat_db_from_session(session, work=work, flat_path=flat_path), session
        except (OSError, ValueError):
            pass

    _log(f"flat-db build-start rtl_files={len(paths)}")
    session = _legacy_prepare(
        paths,
        top=top,
        work_dir=work,
        cache_path=flat_path,
        filelist=filelist,
        index_cwd=index_cwd,
        paths_normalized=True,
        refresh_cache=refresh,
        on_emit=on_log,
    )
    _link_legacy_grep_hie_cache(flat_path=flat_path, legacy_path=legacy_path)
    _log(
        f"flat-db built modules={len(session.module_index)} "
        f"rtl_files={len(session.rtl_paths)} path={flat_path}"
    )
    return _flat_db_from_session(session, work=work, flat_path=flat_path), session