"""Flat DB: module → filepath (wraps hierwalk grep_hie)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from hierwalk.connect.hierarchy_grep_gate import prepare_hierarchy_grep_session as _legacy_prepare
from hierwalk.hierarchy_grep import (
    HierarchyGrepSession,
    dump_grep_hie,
    grep_hie_sources_match,
    load_grep_hie,
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


def load_or_build_flat_db(
    sources: Sequence[str],
    *,
    top: str,
    work_dir: str | Path,
    refresh: bool = False,
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

    paths = [str(Path(p).resolve()) for p in sources if p]

    if flat_path.is_file() and not refresh:
        try:
            cached = load_grep_hie(flat_path)
            if grep_hie_sources_match(cached, paths):
                _log(f"flat-db hit path={flat_path} modules={len(cached.get('module_index', {}))}")
                session = HierarchyGrepSession.from_grep_hie_cache(cached, cache_path=flat_path)
                db = FlatDb(
                    rtl_paths=list(session.rtl_paths),
                    module_index=dict(session.module_index),
                    work_dir=work,
                    path=flat_path,
                )
                return db, session
        except (OSError, ValueError):
            pass

    _log(f"flat-db build-start rtl_files={len(paths)}")
    session = _legacy_prepare(
        paths,
        top=top,
        work_dir=work,
        refresh_cache=refresh,
        on_emit=on_log,
    )
    dump_grep_hie(session, flat_path, top=top)
    _log(
        f"flat-db built modules={len(session.module_index)} "
        f"rtl_files={len(session.rtl_paths)} path={flat_path}"
    )
    db = FlatDb(
        rtl_paths=list(session.rtl_paths),
        module_index=dict(session.module_index),
        work_dir=work,
        path=flat_path,
    )
    return db, session