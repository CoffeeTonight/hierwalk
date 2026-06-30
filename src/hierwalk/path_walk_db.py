"""
Path-walk module DB: Tier-0 regex decl index + Tier-1 validated instance scan.

Built incrementally during path-walk; disk cache per RTL file (regex + validated).
Does not participate in full DesignIndex build / load_or_build_index.

Disk layout (default *cache_dir* = ``.db_{TOP}/`` beside ``--index-cwd`` or cwd)::

    .db_{TOP}/path-walk-db/{cache_key}/regex/{file_token}.pkl
    .db_{TOP}/path-walk-db/{cache_key}/validated/{file_token}_{defines}.pkl
    .db_{TOP}/path-walk-db/{cache_key}/module_index.tsv   (human-readable snapshot)
"""

from __future__ import annotations

import hashlib
import os
import pickle
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from hierwalk.ignore_path import source_path_matches
from hierwalk.index import _module_name_ignored
from hierwalk.index import (
    DesignIndex,
    _ctx_key,
    _module_header_body,
    _scan_module_body,
    scan_preprocessed,
)

from hierwalk.inst_scan import (
    expand_inst_names,
    find_hierarchy_instance,
    find_instance_by_child_module,
    instance_edge_matches_leaf,
    probe_inst_leaf_regex_fast,
    scan_hierarchy_instances,
)
from hierwalk.params import parse_param_pairs
from hierwalk.manifest import LazyPathDigests, PathDigests, path_content_digest
from hierwalk.models import InstanceEdge, ModuleRecord
from hierwalk.params import resolve_param_map

PATH_WALK_DB_VERSION = 12

_PARALLEL_MIN_TIER0 = 4

_MODULE_DECL_RE = re.compile(
    r"^\s*(?:module|interface|program)\s+([A-Za-z_]\w*)\b",
    re.MULTILINE | re.IGNORECASE,
)


@dataclass(frozen=True)
class _FileRegexCacheEntry:
    content_digest: str
    module_names: Tuple[str, ...]


@dataclass(frozen=True)
class _FileValidatedCacheEntry:
    content_digest: str
    defines_digest: str
    modules: Tuple[Tuple[str, ModuleRecord], ...]
    include_closure_digest: str = ""


@dataclass(frozen=True)
class _FilePreprocessedCacheEntry:
    content_digest: str
    defines_digest: str
    include_closure_digest: str
    text: str


InstLeafIndexKey = Tuple[str, str]


def _defines_digest(defines: Mapping[str, str]) -> str:
    hasher = hashlib.sha256()
    for key in sorted(defines):
        hasher.update(key.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(str(defines[key]).encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()[:16]


def _file_cache_token(path: str) -> str:
    return hashlib.sha256(str(Path(path).resolve()).encode("utf-8")).hexdigest()[:20]


def path_walk_db_cache_key(
    sources: Sequence[str | Path],
    *,
    defines: Mapping[str, str],
    include_dirs: Sequence[str | Path] = (),
    skip_path_patterns: Sequence[str] = (),
    path_digests: Optional[Mapping[str, str]] = None,
) -> str:
    """Stable namespace for path-walk DB sidecars (independent of full-index cache)."""
    hasher = hashlib.sha256()
    hasher.update(f"pw-db-v={PATH_WALK_DB_VERSION}".encode())
    lazy_key = isinstance(path_digests, LazyPathDigests)
    for raw in sorted({str(Path(s).resolve()) for s in sources}):
        hasher.update(raw.encode())
        hasher.update(b"\0")
        if not lazy_key:
            digest = path_content_digest(Path(raw), path_digests=path_digests)
            if digest is not None:
                hasher.update(digest.encode())
    for raw in sorted({str(Path(p).resolve()) for p in include_dirs}):
        hasher.update(b"inc:")
        hasher.update(raw.encode())
        hasher.update(b"\0")
    for pat in sorted(set(skip_path_patterns)):
        hasher.update(b"skip:")
        hasher.update(pat.encode())
        hasher.update(b"\0")
    hasher.update(_defines_digest(defines).encode())
    return hasher.hexdigest()


@dataclass(frozen=True)
class _Tier0ScanJob:
    path: str
    cache_root: str
    content_digest: str
    skip_patterns: Tuple[str, ...]


@dataclass(frozen=True)
class _Tier0ScanResult:
    path: str
    names: Tuple[str, ...]
    from_cache: bool
    skipped: bool
    read_failed: bool


def _tier0_regex_sidecar_path(cache_root: str, path: str) -> Path:
    return Path(cache_root) / "regex" / f"{_file_cache_token(path)}.pkl"


def _tier0_load_sidecar_worker(
    cache_root: str,
    path: str,
    content_digest: str,
) -> Optional[Tuple[str, ...]]:
    if not cache_root:
        return None
    sidecar = _tier0_regex_sidecar_path(cache_root, path)
    if not sidecar.is_file():
        return None
    try:
        with sidecar.open("rb") as fh:
            obj = pickle.load(fh)
    except (OSError, pickle.PickleError, EOFError, ValueError):
        return None
    if not isinstance(obj, _FileRegexCacheEntry):
        return None
    if not content_digest or content_digest != obj.content_digest:
        return None
    return tuple(obj.module_names)


def _tier0_save_sidecar_worker(
    cache_root: str,
    path: str,
    content_digest: str,
    names: Sequence[str],
) -> None:
    if not cache_root or not content_digest:
        return
    sidecar = _tier0_regex_sidecar_path(cache_root, path)
    entry = _FileRegexCacheEntry(content_digest, tuple(names))
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
    with tmp.open("wb") as fh:
        pickle.dump(entry, fh, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(sidecar)


def _tier0_worker_scan(job: _Tier0ScanJob) -> _Tier0ScanResult:
    """Process-pool worker: read one RTL file, harvest decl names, persist regex sidecar."""
    key = str(Path(job.path).resolve())
    if job.skip_patterns and source_path_matches(key, job.skip_patterns):
        return _Tier0ScanResult(key, (), False, True, False)

    cached = _tier0_load_sidecar_worker(job.cache_root, key, job.content_digest)
    if cached is not None:
        return _Tier0ScanResult(key, cached, True, False, False)

    try:
        text = Path(key).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return _Tier0ScanResult(key, (), False, False, True)

    names = tuple(tier0_regex_module_names(text))
    _tier0_save_sidecar_worker(job.cache_root, key, job.content_digest, names)
    return _Tier0ScanResult(key, names, False, False, False)


def _resolve_pw_db_jobs(jobs: int, num_tasks: int) -> int:
    if jobs < 0:
        return 1
    if jobs == 0:
        cpu = os.cpu_count() or 1
        return max(1, min(cpu, num_tasks))
    return max(1, min(jobs, num_tasks))


def tier0_regex_module_names(text: str) -> List[str]:
    """Fast declaration harvest (no preprocess). May include ifdef-gated names."""
    names: List[str] = []
    seen: Set[str] = set()
    for m in _MODULE_DECL_RE.finditer(text):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _record_lite(rec: ModuleRecord) -> ModuleRecord:
    return ModuleRecord(
        module_name=rec.module_name,
        file_path=rec.file_path,
        body="",
        raw_params=dict(rec.raw_params),
        instances=list(rec.instances),
        needs_generate_fold=rec.needs_generate_fold,
        is_blackbox=rec.is_blackbox,
        is_interface=rec.is_interface,
        stop_reason=rec.stop_reason,
    )


def _is_placeholder_module(rec: Optional[ModuleRecord]) -> bool:
    if rec is None:
        return True
    if rec.is_blackbox or rec.stop_reason:
        return True
    return not bool(rec.file_path)


RESOLVE_CONFIDENT = "confident"
RESOLVE_RECOVERY = "recovery"
ResolvePolicy = str


@dataclass(frozen=True)
class DeferredResolve:
    """Work item for recovery-pass module/edge resolution."""

    kind: str
    module_name: str
    scope_anchor: str
    inst_leaf: str = ""
    parent_ctx: Tuple[Tuple[str, str], ...] = ()
    expect_inst: Optional[Tuple[str, str]] = None
    child_module: str = ""
    target_path: str = ""
    reset_path: str = ""
    reason: str = ""


def _parent_ctx_key(ctx: Optional[Mapping[str, str]]) -> Tuple[Tuple[str, str], ...]:
    if not ctx:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in ctx.items()))


class PathWalkModuleDb:
    """
    Incremental module→files map (Tier 0) and per-file validated scan (Tier 1).

    Tier 1 uses light preprocess + instance scan; Tier 0 hits are always confirmed
    before use.
    """

    def __init__(
        self,
        sources: Sequence[str | Path],
        index: DesignIndex,
        *,
        include_dirs: Sequence[str | Path] = (),
        defines: Optional[Mapping[str, str]] = None,
        skip_path_patterns: Sequence[str] = (),
        ignore_module_patterns: Sequence[str] = (),
        cache_dir: Optional[Path] = None,
        cache_key: Optional[str] = None,
        no_cache: bool = False,
        on_trace: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[str], None]] = None,
        file_via_filelist: Optional[Mapping[str, str]] = None,
        filelist_children: Optional[Mapping[str, Sequence[str]]] = None,
        path_digests: Optional[Mapping[str, str]] = None,
        jobs: int = 0,
        tier1_prefetch: Optional[bool] = None,
    ) -> None:
        self._sources = [str(Path(s).resolve()) for s in sources]
        if path_digests is None:
            self._path_digests: Optional[Union[PathDigests, LazyPathDigests]] = None
        elif isinstance(path_digests, LazyPathDigests):
            self._path_digests = path_digests
        else:
            self._path_digests = dict(path_digests)
        self._index = index
        self._include_dirs = [Path(p) for p in include_dirs]
        self._defines = dict(defines or {})
        self._skip = tuple(skip_path_patterns)
        self._ignore_modules = tuple(ignore_module_patterns)
        self._no_cache = no_cache
        self._defines_digest = _defines_digest(self._defines)
        self._on_trace = on_trace
        self._on_progress = on_progress
        self._file_via_filelist: Dict[str, str] = dict(file_via_filelist or {})
        self._filelist_children: Dict[str, List[str]] = {
            str(Path(k).resolve()): [str(Path(c).resolve()) for c in v]
            for k, v in (filelist_children or {}).items()
        }
        self._listing_siblings: Dict[str, Tuple[str, ...]] = (
            self._build_listing_siblings()
        )
        self._sources_by_listing: Dict[str, Set[str]] = self._build_sources_by_listing()
        self._listing_ancestors: Dict[str, Tuple[str, ...]] = (
            self._build_listing_ancestors()
        )
        self._should_retry_cache: Dict[str, bool] = {}
        self._scoped_pool_cache: Dict[Tuple[str, str], Tuple[str, ...]] = {}
        self._defer_queue_keys: Set[
            Tuple[str, str, str, str, Optional[Tuple[str, str]]]
        ] = set()
        self._phase = "ready"
        self._last_heartbeat: float = 0.0

        base = cache_dir
        if base is None and not no_cache:
            from hierwalk.cache import default_cache_dir, get_active_work_dir

            active = get_active_work_dir()
            base = active if active is not None else default_cache_dir()
        self._cache_root: Optional[Path] = None
        if base is not None and cache_key and not no_cache:
            self._cache_root = Path(base) / "path-walk-db" / cache_key

        self._module_to_files: Dict[str, List[str]] = {}
        self._file_to_modules: Dict[str, List[str]] = {}
        self._prefer_file: Dict[str, str] = {}
        self._regex_scanned: Set[str] = set()
        self._regex_queue: List[str] = []
        self._validated_memory: Dict[str, Dict[str, ModuleRecord]] = {}
        self.files_regex_scanned: int = 0
        self.files_validated: int = 0
        self.cache_regex_hits: int = 0
        self.cache_validated_hits: int = 0
        self._folded_edges_cache: Dict[Tuple[str, str, str], List[InstanceEdge]] = {}
        self._preprocessed_text_cache: Dict[Tuple[str, str, str], str] = {}
        self._inst_leaf_index: Dict[InstLeafIndexKey, Dict[str, InstanceEdge]] = {}
        self._tier1_warm_inflight: Set[str] = set()
        self._snapshot_dirty: bool = False
        self._snapshot_written: bool = False
        self._jobs = jobs
        self._tier0_executor: Optional[Union[ProcessPoolExecutor, ThreadPoolExecutor]] = None
        self._tier0_inflight: Dict[str, Future[_Tier0ScanResult]] = {}
        from hierwalk.perf import pw_db_prefetch_enabled

        self._tier1_prefetch = (
            tier1_prefetch if tier1_prefetch is not None else pw_db_prefetch_enabled()
        )
        self._tier1_prefetch_thread: Optional[threading.Thread] = None
        self._tier1_scan_lock = threading.Lock()
        self._defer_queue: List[DeferredResolve] = []
        self._defer_seen: Set[Tuple[str, str, str, str, Optional[Tuple[str, str]]]] = set()

    @property
    def cache_root(self) -> Optional[Path]:
        return self._cache_root

    def _trace(self, message: str) -> None:
        if self._on_trace is not None and message:
            self._on_trace(message)

    def _set_phase(self, phase: str, *, detail: str = "") -> None:
        if not phase or phase == self._phase:
            self._maybe_heartbeat()
            return
        self._phase = phase
        self._maybe_heartbeat()
        if self._on_progress is None:
            return
        msg = f"path-walk: {phase}"
        if detail:
            msg += f" — {detail}"
        self._on_progress(msg)

    def _maybe_heartbeat(self) -> None:
        from hierwalk.perf import pw_heartbeat_interval_sec

        interval = pw_heartbeat_interval_sec()
        if interval is None:
            return
        now = time.monotonic()
        if now - self._last_heartbeat < interval:
            return
        self._last_heartbeat = now
        detail = self.heartbeat_detail()
        from hierwalk.perf import pw_trace_verbose

        if pw_trace_verbose():
            self._trace(
                f"pw-db heartbeat tier0={self.files_regex_scanned} "
                f"tier1={self.files_validated} phase={detail or 'idle'}"
            )
        else:
            self._trace(f"pw-db heartbeat phase={detail or 'idle'}")

    def heartbeat_detail(self) -> str:
        return self._phase

    def format_status_line(self) -> str:
        from hierwalk.perf import pw_trace_verbose

        root = str(self._cache_root) if self._cache_root else "(memory only)"
        mapped = len(self._module_to_files)
        base = (
            f"pw-db v{PATH_WALK_DB_VERSION} root={root} "
            f"module_map={mapped} cache_hit="
            f"{self.cache_regex_hits}+{self.cache_validated_hits}"
        )
        if pw_trace_verbose():
            return (
                f"{base} tier0={self.files_regex_scanned} "
                f"tier1={self.files_validated}"
            )
        return base

    def module_to_files_snapshot(self) -> Dict[str, List[str]]:
        return {name: list(files) for name, files in self._module_to_files.items()}

    def write_module_index_snapshot(self, *, force: bool = False) -> Optional[Path]:
        if self._cache_root is None:
            return None
        out = self._cache_root / "module_index.tsv"
        if not force and self._on_trace is None:
            self._snapshot_dirty = True
            return out
        if not force and self._snapshot_written and not self._snapshot_dirty:
            return out
        lines = ["module\tfiles"]
        for name in sorted(self._module_to_files):
            files = self._module_to_files[name]
            lines.append(f"{name}\t{';'.join(files)}")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._snapshot_dirty = False
        self._snapshot_written = True
        return out

    def flush_module_index_snapshot(self) -> Optional[Path]:
        return self.write_module_index_snapshot(force=True)

    def _is_ignored_module(self, module_name: str) -> bool:
        if not module_name:
            return False
        if self._ignore_modules and _module_name_ignored(
            module_name,
            list(self._ignore_modules),
        ):
            return True
        if self._index.module_stop_reason(module_name) == "ignorePath":
            return True
        rec = self._index.get_module(module_name)
        return rec is not None and bool(rec.stop_reason)

    def remember_index_modules(self) -> None:
        for name, rec in self._index.modules.items():
            if self._is_ignored_module(name):
                continue
            if rec.file_path and not _is_placeholder_module(rec):
                self._note_regex_modules(rec.file_path, [name])
                self._prefer_file.setdefault(name, str(Path(rec.file_path).resolve()))

    def seed_top_module(self, top: str, top_file: str) -> None:
        """Register top file path without a full tier1 validated scan."""
        key = str(Path(top_file).resolve())
        self._tier0_scan_file(key)
        rec = self._index.get_module(top)
        if rec is None:
            self._index.modules[top] = ModuleRecord(
                module_name=top,
                file_path=key,
                body="",
                raw_params={},
                instances=[],
                needs_generate_fold=False,
            )
            self._index._rebuild_file_modules()
        elif not rec.file_path:
            self._index.modules[top] = ModuleRecord(
                module_name=top,
                file_path=key,
                body="",
                raw_params=dict(rec.raw_params),
                instances=list(rec.instances),
                needs_generate_fold=rec.needs_generate_fold,
                is_blackbox=rec.is_blackbox,
                is_interface=rec.is_interface,
                stop_reason=rec.stop_reason,
            )
            self._index._rebuild_file_modules()
        self._prefer_file[top] = key
        self._note_regex_modules(key, [top])

    def _source_digest(self, path: str) -> Optional[str]:
        return path_content_digest(Path(path), path_digests=self._path_digests)

    def _regex_sidecar(self, path: str) -> Optional[Path]:
        if self._cache_root is None:
            return None
        return self._cache_root / "regex" / f"{_file_cache_token(path)}.pkl"

    def _validated_sidecar(
        self,
        path: str,
        *,
        defines_digest: str,
    ) -> Optional[Path]:
        if self._cache_root is None:
            return None
        return (
            self._cache_root
            / "validated"
            / f"{_file_cache_token(path)}_{defines_digest}.pkl"
        )

    def _preprocessed_sidecar(
        self,
        path: str,
        *,
        defines_digest: str,
        include_closure_digest: str,
    ) -> Optional[Path]:
        if self._cache_root is None:
            return None
        return (
            self._cache_root
            / "preprocessed"
            / f"{_file_cache_token(path)}_{defines_digest}_{include_closure_digest}.pkl"
        )

    def _include_closure_digest(self, path: str) -> str:
        from hierwalk.preprocess import _collect_include_closure

        closure, _ = _collect_include_closure(
            [path],
            self._include_dirs,
            skip_path_patterns=self._skip,
        )
        hasher = hashlib.sha256()
        for inc in sorted({str(p.resolve()) for p in closure}):
            hasher.update(inc.encode())
            hasher.update(b"\0")
            digest = self._source_digest(inc)
            if digest is not None:
                hasher.update(digest.encode())
            hasher.update(b"\0")
        return hasher.hexdigest()[:16]

    def _load_regex_sidecar(self, path: str) -> Optional[_FileRegexCacheEntry]:
        sidecar = self._regex_sidecar(path)
        if sidecar is None or not sidecar.is_file():
            return None
        try:
            with sidecar.open("rb") as fh:
                obj = pickle.load(fh)
        except (OSError, pickle.PickleError, EOFError, ValueError):
            return None
        if not isinstance(obj, _FileRegexCacheEntry):
            return None
        live = self._source_digest(path)
        if live is None or live != obj.content_digest:
            return None
        return obj

    def _save_regex_sidecar(self, path: str, names: Sequence[str]) -> None:
        sidecar = self._regex_sidecar(path)
        if sidecar is None:
            return
        digest = self._source_digest(path)
        if digest is None:
            return
        entry = _FileRegexCacheEntry(digest, tuple(names))
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
        with tmp.open("wb") as fh:
            pickle.dump(entry, fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(sidecar)

    def _load_validated_sidecar(
        self,
        path: str,
        *,
        defines_digest: str,
        include_closure_digest: str,
    ) -> Optional[Dict[str, ModuleRecord]]:
        sidecar = self._validated_sidecar(path, defines_digest=defines_digest)
        if sidecar is None or not sidecar.is_file():
            return None
        try:
            with sidecar.open("rb") as fh:
                obj = pickle.load(fh)
        except (OSError, pickle.PickleError, EOFError, ValueError):
            return None
        if not isinstance(obj, _FileValidatedCacheEntry):
            return None
        live = self._source_digest(path)
        if live is None or live != obj.content_digest:
            return None
        if obj.defines_digest != defines_digest:
            return None
        if (
            include_closure_digest
            and obj.include_closure_digest != include_closure_digest
        ):
            return None
        return {name: _record_lite(rec) for name, rec in obj.modules}

    def _save_validated_sidecar(
        self,
        path: str,
        modules: Mapping[str, ModuleRecord],
        *,
        defines_digest: str,
        include_closure_digest: str,
    ) -> None:
        sidecar = self._validated_sidecar(path, defines_digest=defines_digest)
        if sidecar is None:
            return
        digest = self._source_digest(path)
        if digest is None:
            return
        entry = _FileValidatedCacheEntry(
            digest,
            defines_digest,
            tuple((n, _record_lite(r)) for n, r in sorted(modules.items())),
            include_closure_digest,
        )
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
        with tmp.open("wb") as fh:
            pickle.dump(entry, fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(sidecar)

    def _load_preprocessed_sidecar(
        self,
        path: str,
        *,
        defines_digest: str,
        include_closure_digest: str,
    ) -> Optional[str]:
        sidecar = self._preprocessed_sidecar(
            path,
            defines_digest=defines_digest,
            include_closure_digest=include_closure_digest,
        )
        if sidecar is None or not sidecar.is_file():
            return None
        try:
            with sidecar.open("rb") as fh:
                obj = pickle.load(fh)
        except (OSError, pickle.PickleError, EOFError, ValueError):
            return None
        if not isinstance(obj, _FilePreprocessedCacheEntry):
            return None
        live = self._source_digest(path)
        if live is None or live != obj.content_digest:
            return None
        if obj.defines_digest != defines_digest:
            return None
        if obj.include_closure_digest != include_closure_digest:
            return None
        return obj.text

    def _save_preprocessed_sidecar(
        self,
        path: str,
        text: str,
        *,
        defines_digest: str,
        include_closure_digest: str,
    ) -> None:
        if self._no_cache:
            return
        sidecar = self._preprocessed_sidecar(
            path,
            defines_digest=defines_digest,
            include_closure_digest=include_closure_digest,
        )
        if sidecar is None:
            return
        digest = self._source_digest(path)
        if digest is None:
            return
        entry = _FilePreprocessedCacheEntry(
            digest,
            defines_digest,
            include_closure_digest,
            text,
        )
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
        with tmp.open("wb") as fh:
            pickle.dump(entry, fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(sidecar)

    def _inst_index_key(
        self,
        parent_module: str,
        parent_ctx: Mapping[str, str],
    ) -> InstLeafIndexKey:
        rec = self._index.get_module(parent_module)
        raw = dict(rec.raw_params) if rec else {}
        pmap = resolve_param_map(raw, parent=parent_ctx)
        return parent_module, _ctx_key(pmap)

    def _register_inst_edges(
        self,
        parent_module: str,
        parent_ctx: Mapping[str, str],
        edges: Sequence[InstanceEdge],
    ) -> None:
        if not edges:
            return
        key = self._inst_index_key(parent_module, parent_ctx)
        rec = self._index.get_module(parent_module)
        raw = dict(rec.raw_params) if rec else {}
        pmap = resolve_param_map(raw, parent=parent_ctx)
        bucket = self._inst_leaf_index.setdefault(key, {})
        for edge in edges:
            for name in expand_inst_names(edge.inst_name, "", pmap):
                bucket[name] = edge
                bucket[name.lower()] = edge

    def _invalidate_inst_leaf_index(
        self,
        mod_names: Optional[Set[str]] = None,
    ) -> None:
        if not mod_names:
            self._inst_leaf_index.clear()
            return
        drop = [key for key in self._inst_leaf_index if key[0] in mod_names]
        for key in drop:
            del self._inst_leaf_index[key]

    def lookup_inst_edge(
        self,
        parent_module: str,
        parent_ctx: Mapping[str, str],
        inst_leaf: str,
    ) -> Optional[InstanceEdge]:
        """O(1) child-edge lookup by instance leaf name (incl. array indices)."""
        if not inst_leaf:
            return None
        bucket = self._inst_leaf_index.get(self._inst_index_key(parent_module, parent_ctx))
        if not bucket:
            return None
        edge = bucket.get(inst_leaf) or bucket.get(inst_leaf.lower())
        if edge is not None:
            return edge
        base = inst_leaf.split("[", 1)[0]
        if base != inst_leaf:
            edge = bucket.get(base) or bucket.get(base.lower())
            if edge is not None:
                return edge
        return None

    @staticmethod
    def _name_prefix_matches_remainder(remainder: str, inst_name: str) -> bool:
        if remainder == inst_name:
            return True
        prefix = inst_name + "."
        return (
            remainder.startswith(prefix)
            or remainder.lower().startswith(prefix.lower())
        )

    def longest_inst_prefix_match(
        self,
        parent_module: str,
        parent_ctx: Mapping[str, str],
        remainder: str,
    ) -> Tuple[str, Optional[InstanceEdge]]:
        """Longest instance-name prefix of *remainder* using the inst-leaf index."""
        bucket = self._inst_leaf_index.get(self._inst_index_key(parent_module, parent_ctx))
        if not bucket or not remainder:
            return "", None
        best_name = ""
        best_edge: Optional[InstanceEdge] = None
        seen: Set[int] = set()
        for name, edge in bucket.items():
            eid = id(edge)
            if eid in seen:
                continue
            if not self._name_prefix_matches_remainder(remainder, name):
                continue
            seen.add(eid)
            if len(name) > len(best_name):
                best_name = name
                best_edge = edge
        return best_name, best_edge

    def _note_regex_modules(self, path: str, names: Iterable[str]) -> None:
        key = str(Path(path).resolve())
        file_names = self._file_to_modules.setdefault(key, [])
        for name in names:
            if not name or self._is_ignored_module(name):
                continue
            if name not in file_names:
                file_names.append(name)
            files = self._module_to_files.setdefault(name, [])
            if key not in files:
                files.append(key)

    def _listing_for_rtl(self, rtl_file: str) -> str:
        if not rtl_file:
            return ""
        anchor = str(Path(rtl_file).resolve())
        listing = self._file_via_filelist.get(anchor, "")
        if not listing:
            listing = self._index.filelist_for(anchor) or ""
        return listing

    def _scoped_sources_for_listings(
        self,
        listings: Sequence[str],
        *,
        include_anchor_rtl: str = "",
    ) -> List[str]:
        reachable = {str(Path(fl).resolve()) for fl in listings if fl}
        scoped: List[str] = []
        for src in self._sources:
            key = str(Path(src).resolve())
            via = self._file_via_filelist.get(key, "") or self._index.filelist_for(key)
            if via in reachable:
                scoped.append(key)
        if include_anchor_rtl:
            anchor = str(Path(include_anchor_rtl).resolve())
            if anchor not in scoped and anchor in self._sources:
                scoped.insert(0, anchor)
        return scoped

    def _scoped_child_sources_for_rtl(self, rtl_file: str) -> List[str]:
        """
        RTL listed only in *direct* child filelists of the listing that contains *rtl_file*.

        Confident resolve assumes instances live under child lists, not parent/sibling FLs.
        """
        listing = self._listing_for_rtl(rtl_file)
        if not listing:
            anchor = str(Path(rtl_file).resolve()) if rtl_file else ""
            return [anchor] if anchor in self._sources else []
        child_listings = [
            str(Path(fl).resolve())
            for fl in self._filelist_children.get(listing, ())
        ]
        if not child_listings:
            return self._scoped_sources_for_listings([listing], include_anchor_rtl=rtl_file)
        return self._scoped_sources_for_listings(child_listings)

    def _scoped_pool_for_policy(
        self,
        rtl_file: str,
        *,
        policy: ResolvePolicy,
    ) -> Tuple[str, ...]:
        if not rtl_file:
            return tuple(self._sources)
        anchor_key = str(Path(rtl_file).resolve())
        cache_key = (anchor_key, policy)
        cached = self._scoped_pool_cache.get(cache_key)
        if cached is not None:
            return cached
        if policy == RESOLVE_CONFIDENT:
            pool = self._scoped_child_sources_for_rtl(rtl_file)
        else:
            pool = self._scoped_sources_for_rtl(rtl_file)
        cached = tuple(pool)
        self._scoped_pool_cache[cache_key] = cached
        return cached

    def _scoped_sources_for_rtl(self, rtl_file: str) -> List[str]:
        """
        RTL sources reachable from the filelist subtree that lists *rtl_file*.

        Search starts here (not the whole design) so duplicate module decls in
        unrelated filelists are not preferred over the parent's listing chain.
        """
        if not rtl_file:
            return list(self._sources)
        listing = self._listing_for_rtl(rtl_file)
        if not listing:
            anchor = str(Path(rtl_file).resolve())
            return [anchor] if anchor in self._sources else list(self._sources)

        reachable: Set[str] = {listing}
        queue = [listing]
        while queue:
            fl = queue.pop()
            for child in self._filelist_children.get(fl, ()):
                if child not in reachable:
                    reachable.add(child)
                    queue.append(child)

        scoped = self._scoped_sources_for_listings(reachable, include_anchor_rtl=rtl_file)
        return scoped or list(self._sources)

    @staticmethod
    def _defer_item_key(
        item: DeferredResolve,
    ) -> Tuple[str, str, str, str, Optional[Tuple[str, str]]]:
        return (
            item.kind,
            item.module_name,
            item.inst_leaf,
            item.scope_anchor,
            item.expect_inst,
        )

    def _build_sources_by_listing(self) -> Dict[str, Set[str]]:
        out: Dict[str, Set[str]] = {}
        for src in self._sources:
            key = str(Path(src).resolve())
            via = self._file_via_filelist.get(key, "") or self._index.filelist_for(key)
            if not via:
                continue
            listing_key = str(Path(via).resolve())
            out.setdefault(listing_key, set()).add(key)
        return out

    def _build_listing_ancestors(self) -> Dict[str, Tuple[str, ...]]:
        parents: Dict[str, List[str]] = {}
        all_listings: Set[str] = set()
        for parent, kids in self._filelist_children.items():
            parent_key = str(Path(parent).resolve())
            all_listings.add(parent_key)
            for kid in kids:
                child_key = str(Path(kid).resolve())
                all_listings.add(child_key)
                parents.setdefault(child_key, []).append(parent_key)
        all_listings.update(self._sources_by_listing.keys())
        out: Dict[str, Tuple[str, ...]] = {}
        for listing in all_listings:
            resolved = str(Path(listing).resolve())
            ancestors: Set[str] = {resolved}
            queue = [resolved]
            while queue:
                fl = queue.pop()
                for parent in parents.get(fl, ()):
                    if parent not in ancestors:
                        ancestors.add(parent)
                        queue.append(parent)
            out[resolved] = tuple(sorted(ancestors))
        return out

    def _build_listing_siblings(self) -> Dict[str, Tuple[str, ...]]:
        siblings: Dict[str, Set[str]] = {}
        for kids in self._filelist_children.values():
            kid_keys = [str(Path(k).resolve()) for k in kids]
            for key in kid_keys:
                siblings.setdefault(key, set()).update(
                    s for s in kid_keys if s != key
                )
        return {k: tuple(sorted(v)) for k, v in siblings.items()}

    def _ancestor_listings(self, listing: str) -> Tuple[str, ...]:
        """Filelists that transitively include *listing* (parents up to root)."""
        if not listing:
            return ()
        resolved = str(Path(listing).resolve())
        cached = self._listing_ancestors.get(resolved)
        if cached is not None:
            return cached
        parents: Dict[str, List[str]] = {}
        for parent, kids in self._filelist_children.items():
            parent_key = str(Path(parent).resolve())
            for kid in kids:
                child_key = str(Path(kid).resolve())
                parents.setdefault(child_key, []).append(parent_key)
        ancestors: Set[str] = {resolved}
        queue = [resolved]
        while queue:
            fl = queue.pop()
            for parent in parents.get(fl, ()):
                if parent not in ancestors:
                    ancestors.add(parent)
                    queue.append(parent)
        result = tuple(sorted(ancestors))
        self._listing_ancestors[resolved] = result
        return result

    def _sources_for_listing(self, listing: str) -> Set[str]:
        listing_key = str(Path(listing).resolve())
        hit = self._sources_by_listing.get(listing_key)
        return set(hit) if hit is not None else set()

    def _descendant_listings(self, listing: str) -> Set[str]:
        resolved = str(Path(listing).resolve()) if listing else ""
        if not resolved:
            return set()
        out: Set[str] = {resolved}
        queue = [resolved]
        while queue:
            fl = queue.pop()
            for child in self._filelist_children.get(fl, ()):
                if child not in out:
                    out.add(child)
                    queue.append(child)
        return out

    def _listing_depth_from(self, ancestor: str, descendant: str) -> int:
        anc = str(Path(ancestor).resolve())
        desc = str(Path(descendant).resolve())
        if anc == desc:
            return 0
        queue: List[Tuple[str, int]] = [(anc, 0)]
        visited = {anc}
        while queue:
            fl, depth = queue.pop(0)
            for child in self._filelist_children.get(fl, ()):
                if child == desc:
                    return depth + 1
                if child not in visited:
                    visited.add(child)
                    queue.append((child, depth + 1))
        return 99

    def _filelist_proximity(self, anchor_rtl: str, candidate_rtl: str) -> int:
        """Lower score means closer in filelist / hierarchy."""
        if not anchor_rtl:
            return 0
        anchor_key = str(Path(anchor_rtl).resolve())
        cand_key = str(Path(candidate_rtl).resolve())
        if anchor_key == cand_key:
            return 0

        anchor_listing = self._listing_for_rtl(anchor_rtl)
        cand_listing = self._listing_for_rtl(candidate_rtl)
        if not anchor_listing:
            return 500 if cand_listing else 1000
        if not cand_listing:
            return 400

        anchor_fl = str(Path(anchor_listing).resolve())
        cand_fl = str(Path(cand_listing).resolve())
        if anchor_fl == cand_fl:
            return 1

        descendants = self._descendant_listings(anchor_fl)
        if cand_fl in descendants and cand_fl != anchor_fl:
            return 2 + self._listing_depth_from(anchor_fl, cand_fl)

        ancestors = set(self._ancestor_listings(anchor_fl))
        if cand_fl in ancestors and cand_fl != anchor_fl:
            return 10 + self._listing_depth_from(cand_fl, anchor_fl)

        if cand_fl in self._listing_siblings.get(anchor_fl, ()):
            return 25

        cand_ancestors = set(self._ancestor_listings(cand_fl))
        shared = ancestors & cand_ancestors
        if shared:
            best = 99
            for lca in shared:
                dist = self._listing_depth_from(lca, anchor_fl) + self._listing_depth_from(
                    lca, cand_fl
                )
                best = min(best, dist)
            return 40 + best

        anchor_chain = self._index.filelist_chain_for(anchor_rtl) or ""
        cand_chain = self._index.filelist_chain_for(candidate_rtl) or ""
        if anchor_chain and cand_chain:
            prefix = os.path.commonprefix([anchor_chain, cand_chain])
            if prefix:
                return 60 + abs(len(anchor_chain) - len(cand_chain)) - len(prefix)

        return 1000

    @staticmethod
    def _normalize_name_hint(name: str) -> str:
        n = name.strip().lower()
        if n.startswith("u_") and len(n) > 2:
            return n[2:]
        return n

    def _name_hints(
        self,
        module_name: str = "",
        inst_leaf: str = "",
    ) -> Tuple[str, ...]:
        hints: List[str] = []
        for raw in (module_name, inst_leaf):
            if not raw:
                continue
            hints.append(raw)
            norm = self._normalize_name_hint(raw)
            if norm and norm not in hints:
                hints.append(norm)
        return tuple(hints)

    def _rtl_name_similarity(self, rtl_path: str, hints: Sequence[str]) -> int:
        key = str(Path(rtl_path).resolve())
        stem = Path(key).stem.lower()
        score = 0
        for hint in hints:
            h = hint.lower()
            if not h:
                continue
            if stem == h:
                score = max(score, 100)
            elif h in stem:
                score = max(score, 70)
            elif stem in h:
                score = max(score, 60)
            elif stem.startswith(h) or h.startswith(stem):
                score = max(score, 40)
        for mod in self._file_to_modules.get(key, ()):
            ml = mod.lower()
            for hint in hints:
                h = hint.lower()
                if not h:
                    continue
                if ml == h:
                    score = max(score, 120)
                elif h in ml or ml in h:
                    score = max(score, 80)
        return score

    def _sort_files_by_resolve_rank(
        self,
        files: Sequence[str],
        *,
        scope_anchor: str = "",
        module_name: str = "",
        inst_leaf: str = "",
    ) -> List[str]:
        ordered = list(dict.fromkeys(str(Path(f).resolve()) for f in files))
        hints = self._name_hints(module_name, inst_leaf)
        if not scope_anchor and not hints:
            return ordered

        def rank(f: str) -> Tuple[int, int, str]:
            prox = (
                self._filelist_proximity(scope_anchor, f)
                if scope_anchor
                else 100
            )
            name_sc = self._rtl_name_similarity(f, hints) if hints else 0
            return (prox, -name_sc, f)

        return sorted(ordered, key=rank)

    def _sort_by_filelist_proximity(
        self,
        files: Sequence[str],
        *,
        scope_anchor: str,
        module_name: str = "",
        inst_leaf: str = "",
    ) -> List[str]:
        return self._sort_files_by_resolve_rank(
            files,
            scope_anchor=scope_anchor,
            module_name=module_name,
            inst_leaf=inst_leaf,
        )

    def _tier0_target_module(
        self,
        module_name: str,
        *,
        policy: ResolvePolicy,
    ) -> str:
        """Recovery keeps scanning after the first decl hit (dup modules)."""
        if (
            policy == RESOLVE_RECOVERY
            and module_name
            and module_name in self._module_to_files
        ):
            return ""
        return module_name

    def should_retry_deferred_recovery(self, scope_anchor: str) -> bool:
        """
        True when recovery policy may succeed where confident resolution could not.

        Recovery can differ from confident on the same scoped pool (subtree vs child
        filelist, tier0 ordering). Also true when extra RTL is co-listed on an
        ancestor filelist or a sibling filelist of the anchor's listing — not for
        unrelated parallel branches deeper in the hierarchy.
        """
        if not scope_anchor:
            return False
        anchor_key = str(Path(scope_anchor).resolve())
        cached = self._should_retry_cache.get(anchor_key)
        if cached is not None:
            return cached
        conf = set(self._scoped_pool_for_policy(scope_anchor, policy=RESOLVE_CONFIDENT))
        rec = set(self._scoped_pool_for_policy(scope_anchor, policy=RESOLVE_RECOVERY))
        if rec != conf:
            self._should_retry_cache[anchor_key] = True
            return True
        if len(self._sources) <= len(conf):
            self._should_retry_cache[anchor_key] = False
            return False
        extra = set(self._sources) - conf
        if not extra:
            self._should_retry_cache[anchor_key] = False
            return False
        listing = self._listing_for_rtl(scope_anchor)
        if not listing:
            self._should_retry_cache[anchor_key] = False
            return False
        listing_key = str(Path(listing).resolve())
        for anc in self._ancestor_listings(listing):
            if self._sources_for_listing(anc) & extra:
                self._should_retry_cache[anchor_key] = True
                return True
        for sibling_key in self._listing_siblings.get(listing_key, ()):
            if self._sources_for_listing(sibling_key) & extra:
                self._should_retry_cache[anchor_key] = True
                return True
        self._should_retry_cache[anchor_key] = False
        return False

    def defer_count(self) -> int:
        return len(self._defer_queue)

    def take_defer_queue(self) -> List[DeferredResolve]:
        items = list(self._defer_queue)
        self._defer_queue.clear()
        self._defer_queue_keys.clear()
        return items

    def requeue_defer(self, item: DeferredResolve) -> None:
        """Restore a defer item skipped during recovery (keeps ``_defer_seen``)."""
        key = self._defer_item_key(item)
        if key in self._defer_queue_keys:
            return
        self._defer_queue_keys.add(key)
        self._defer_queue.append(item)

    def _enqueue_defer(self, item: DeferredResolve) -> None:
        key = self._defer_item_key(item)
        if key in self._defer_seen:
            return
        self._defer_seen.add(key)
        self._defer_queue_keys.add(key)
        self._defer_queue.append(item)
        child_fl = ""
        if item.scope_anchor:
            listing = self._listing_for_rtl(item.scope_anchor)
            kids = self._filelist_children.get(listing, ())
            if kids:
                child_fl = Path(str(kids[0])).name
        from hierwalk.hierarchy_log import format_confident_defer_line

        self._trace(
            format_confident_defer_line(
                kind=item.kind,
                module=item.module_name,
                inst_leaf=item.inst_leaf,
                scope_anchor=item.scope_anchor,
                child_filelist=child_fl,
                reason=item.reason,
                target_path=item.target_path,
            )
        )

    def _tier0_executor_get(self) -> Union[ProcessPoolExecutor, ThreadPoolExecutor]:
        if self._tier0_executor is not None:
            return self._tier0_executor
        workers = _resolve_pw_db_jobs(self._jobs, len(self._sources))
        try:
            self._tier0_executor = ProcessPoolExecutor(max_workers=workers)
        except (OSError, PermissionError, RuntimeError):
            self._tier0_executor = ThreadPoolExecutor(max_workers=workers)
        return self._tier0_executor

    def _tier0_make_job(self, path: str) -> _Tier0ScanJob:
        digest = self._source_digest(path) or ""
        cache_root = str(self._cache_root) if self._cache_root is not None else ""
        return _Tier0ScanJob(
            path=path,
            cache_root=cache_root,
            content_digest=digest,
            skip_patterns=tuple(self._skip),
        )

    def _ingest_tier0_result(self, result: _Tier0ScanResult) -> None:
        key = result.path
        if key in self._regex_scanned:
            return
        self._regex_scanned.add(key)
        if result.skipped:
            self.files_regex_scanned += 1
            self._trace(f"pw-db tier0 skip {Path(key).name}")
            return
        if result.read_failed:
            self.files_regex_scanned += 1
            self._trace(f"pw-db tier0 read-fail {Path(key).name}")
            return
        names = list(result.names)
        if result.from_cache:
            self.cache_regex_hits += 1
            self._trace(
                f"pw-db tier0 cache {Path(key).name} -> {','.join(names) or '(none)'}"
            )
        else:
            self._trace(
                f"pw-db tier0 scan {Path(key).name} -> {','.join(names) or '(none)'}"
            )
        self._note_regex_modules(key, names)
        if not result.from_cache and self._cache_root is not None:
            digest = self._source_digest(key)
            if digest is not None:
                self._save_regex_sidecar(key, names)
        self.files_regex_scanned += 1
        self._snapshot_dirty = True

    def _tier0_drain_completed(self, *, block: bool = False, timeout: float = 0.0) -> int:
        if not self._tier0_inflight:
            return 0
        if block and timeout <= 0:
            done, _pending = wait(
                list(self._tier0_inflight.values()),
                return_when=FIRST_COMPLETED,
            )
        elif block:
            done, _pending = wait(
                list(self._tier0_inflight.values()),
                return_when=FIRST_COMPLETED,
                timeout=timeout,
            )
        else:
            done = {f for f in self._tier0_inflight.values() if f.done()}

        ingested = 0
        for fut in done:
            path_key = ""
            for key, pending in list(self._tier0_inflight.items()):
                if pending is fut:
                    path_key = key
                    del self._tier0_inflight[key]
                    break
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001 — worker boundary
                if path_key:
                    self._regex_scanned.add(path_key)
                    self.files_regex_scanned += 1
                    self._trace(f"pw-db tier0 worker-fail {Path(path_key).name}: {exc!r}")
                continue
            self._ingest_tier0_result(result)
            ingested += 1
        return ingested

    def _tier0_submit(self, paths: Sequence[str]) -> int:
        executor = self._tier0_executor_get()
        submitted = 0
        for raw in paths:
            key = str(Path(raw).resolve())
            if key in self._regex_scanned or key in self._tier0_inflight:
                continue
            job = self._tier0_make_job(key)
            self._tier0_inflight[key] = executor.submit(_tier0_worker_scan, job)
            submitted += 1
        return submitted

    def start_background_tier1_prefetch(
        self,
        *,
        sources: Optional[Sequence[str]] = None,
    ) -> int:
        """
        Tier-1 validated scan for files not yet needed by the active walk.

        Opt-in via ``tier1_prefetch`` / ``HIERWALK_PW_DB_PREFETCH=1``.  Warms
        in-memory validated map and disk sidecars for later hierarchy queries.
        """
        if not self._tier1_prefetch or self._tier1_prefetch_thread is not None:
            return 0
        from hierwalk.perf import pw_db_prefetch_max_files

        pool = (
            [str(Path(s).resolve()) for s in sources]
            if sources is not None
            else list(self._sources)
        )
        pending = [
            key
            for key in pool
            if key not in self._validated_memory
            and (not self._skip or not source_path_matches(key, self._skip))
        ]
        cap = pw_db_prefetch_max_files()
        if cap > 0:
            pending = pending[:cap]
        if not pending:
            return 0

        work = list(pending)

        def _run_prefetch() -> None:
            for key in work:
                try:
                    self.tier1_scan_file(key)
                except Exception as exc:  # noqa: BLE001 — background best-effort
                    self._trace(f"pw-db tier1 prefetch-fail {Path(key).name}: {exc!r}")

        self._set_phase("prefetch", detail=f"{len(work)} file(s)")
        self._tier1_prefetch_thread = threading.Thread(
            target=_run_prefetch,
            name="pw-db-tier1-prefetch",
            daemon=True,
        )
        self._tier1_prefetch_thread.start()
        self._trace(f"pw-db tier1 prefetch start ({len(work)} file(s))")
        return len(work)

    def drain_background_workers(self, *, wait_all: bool = True) -> None:
        """Ingest completed tier-0 workers; optionally wait for stragglers (no cancel)."""
        if wait_all:
            while self._tier0_inflight:
                self._tier0_drain_completed(block=True, timeout=0.25)
        else:
            self._tier0_drain_completed(block=False)
        if wait_all and self._tier1_prefetch_thread is not None:
            self._tier1_prefetch_thread.join()
            self._tier1_prefetch_thread = None
        if self._snapshot_dirty:
            self.write_module_index_snapshot()

    def shutdown_workers(self, *, wait: bool = True) -> None:
        self.drain_background_workers(wait_all=wait)
        if self._tier0_executor is not None:
            self._tier0_executor.shutdown(wait=wait, cancel_futures=False)
            self._tier0_executor = None

    def _tier0_scan_sources_parallel(
        self,
        paths: Sequence[str],
        *,
        target_module: str = "",
    ) -> int:
        pending = [
            str(Path(p).resolve())
            for p in paths
            if str(Path(p).resolve()) not in self._regex_scanned
            and str(Path(p).resolve()) not in self._tier0_inflight
        ]
        if not pending:
            self._tier0_drain_completed(block=False)
            return 0

        submitted = self._tier0_submit(pending)
        while self._tier0_inflight:
            self._tier0_drain_completed(block=True, timeout=0.1)
            if target_module and target_module in self._module_to_files:
                return submitted
        return submitted

    def _tier0_scan_sources(
        self,
        sources: Sequence[str],
        *,
        target_module: str = "",
    ) -> int:
        keys = [str(Path(src).resolve()) for src in sources]
        pending = [k for k in keys if k not in self._regex_scanned and k not in self._tier0_inflight]
        if not pending:
            self._tier0_drain_completed(block=False)
            return 0

        workers = _resolve_pw_db_jobs(self._jobs, len(pending))
        if workers <= 1 or len(pending) < _PARALLEL_MIN_TIER0:
            added = 0
            for key in pending:
                self._tier0_scan_file(key)
                added += 1
                if target_module and target_module in self._module_to_files:
                    return added
            return added

        return self._tier0_scan_sources_parallel(pending, target_module=target_module)

    def _tier0_scan_file(self, path: str) -> List[str]:
        key = str(Path(path).resolve())
        if self._skip and source_path_matches(key, self._skip):
            if key not in self._regex_scanned:
                self._regex_scanned.add(key)
                self.files_regex_scanned += 1
                self._trace(f"pw-db tier0 skip {Path(key).name}")
            return []
        if key in self._regex_scanned:
            return list(self._file_to_modules.get(key, []))
        self._set_phase("mapping", detail=Path(key).name)
        self._regex_scanned.add(key)

        hit = self._load_regex_sidecar(key)
        if hit is not None:
            self.cache_regex_hits += 1
            names = list(hit.module_names)
            self._note_regex_modules(key, names)
            self.files_regex_scanned += 1
            self._trace(
                f"pw-db tier0 cache {Path(key).name} -> {','.join(names) or '(none)'}"
            )
            return names

        try:
            text = Path(key).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            self.files_regex_scanned += 1
            self._trace(f"pw-db tier0 read-fail {Path(key).name}")
            return []
        names = tier0_regex_module_names(text)
        self._note_regex_modules(key, names)
        self._save_regex_sidecar(key, names)
        self.files_regex_scanned += 1
        self._trace(
            f"pw-db tier0 scan {Path(key).name} -> {','.join(names) or '(none)'}"
        )
        return names

    def _scan_remaining_sources_tier0(
        self,
        sources: Optional[Sequence[str]] = None,
        *,
        target_module: str = "",
        scope_anchor: str = "",
        policy: ResolvePolicy = RESOLVE_RECOVERY,
    ) -> int:
        """Tier-0 scan sources not yet regex-indexed (scoped pool or full design)."""
        pool = (
            [str(Path(s).resolve()) for s in sources]
            if sources is not None
            else list(self._sources)
        )
        pending = [
            src
            for src in pool
            if src not in self._regex_scanned and src not in self._tier0_inflight
        ]
        if pending:
            pending = self._sort_files_by_resolve_rank(
                pending,
                scope_anchor=scope_anchor,
                module_name=target_module,
            )
        return self._tier0_scan_sources(
            pending,
            target_module=self._tier0_target_module(target_module, policy=policy),
        )

    def _sort_module_files(
        self,
        module_name: str,
        files: Sequence[str],
        *,
        scope_anchor: str = "",
        policy: ResolvePolicy = RESOLVE_CONFIDENT,
        scope_pool: Optional[Sequence[str]] = None,
        inst_leaf: str = "",
    ) -> List[str]:
        ordered = list(dict.fromkeys(str(Path(f).resolve()) for f in files))
        preferred = self._prefer_file.get(module_name)
        if preferred and preferred in ordered:
            ordered = [preferred] + [f for f in ordered if f != preferred]
        if scope_anchor:
            if scope_pool is None:
                scope_pool = self._scoped_pool_for_policy(scope_anchor, policy=policy)
            scoped = set(scope_pool)
            in_scope = [f for f in ordered if f in scoped]
            out_scope = [f for f in ordered if f not in scoped]
            in_scope = self._sort_files_by_resolve_rank(
                in_scope,
                scope_anchor=scope_anchor,
                module_name=module_name,
                inst_leaf=inst_leaf,
            )
            out_scope = self._sort_files_by_resolve_rank(
                out_scope,
                scope_anchor=scope_anchor,
                module_name=module_name,
                inst_leaf=inst_leaf,
            )
            ordered = in_scope + out_scope if policy == RESOLVE_RECOVERY else in_scope
        elif module_name or inst_leaf:
            ordered = self._sort_files_by_resolve_rank(
                ordered,
                module_name=module_name,
                inst_leaf=inst_leaf,
            )
        return ordered

    def _ensure_regex_candidates(
        self,
        module_name: str,
        *,
        scope_anchor: str = "",
        policy: ResolvePolicy = RESOLVE_CONFIDENT,
        inst_leaf: str = "",
    ) -> List[str]:
        from hierwalk.perf import pw_module_file_cap, pw_tier0_global_scan_max

        if self._is_ignored_module(module_name):
            return []

        if scope_anchor:
            scoped_pool = self._scoped_pool_for_policy(scope_anchor, policy=policy)
            if scoped_pool:
                self._tier0_scan_sources(
                    scoped_pool,
                    target_module=self._tier0_target_module(
                        module_name,
                        policy=policy,
                    ),
                )
                if module_name in self._module_to_files and policy == RESOLVE_CONFIDENT:
                    files = list(self._module_to_files.get(module_name, []))
                    return self._sort_module_files(
                        module_name,
                        files,
                        scope_anchor=scope_anchor,
                        policy=policy,
                        scope_pool=scoped_pool,
                        inst_leaf=inst_leaf,
                    )
            if policy == RESOLVE_CONFIDENT:
                return []

        if not self._regex_queue:
            pending = [
                s
                for s in self._sources
                if s not in self._regex_scanned and s not in self._tier0_inflight
            ]
            pending = self._sort_files_by_resolve_rank(
                pending,
                scope_anchor=scope_anchor,
                module_name=module_name,
                inst_leaf=inst_leaf,
            )
            self._regex_queue = pending

        workers = _resolve_pw_db_jobs(self._jobs, len(self._sources))
        batch_size = max(workers * 4, _PARALLEL_MIN_TIER0)
        scan_cap = (
            pw_module_file_cap()
            if policy == RESOLVE_CONFIDENT
            else pw_tier0_global_scan_max()
        )
        files_scanned = 0
        while self._regex_queue and files_scanned < scan_cap:
            if policy == RESOLVE_CONFIDENT and module_name in self._module_to_files:
                break
            batch: List[str] = []
            while self._regex_queue and len(batch) < batch_size:
                nxt = self._regex_queue.pop(0)
                key = str(Path(nxt).resolve())
                if key in self._regex_scanned or key in self._tier0_inflight:
                    continue
                batch.append(key)
            if not batch:
                self._tier0_drain_completed(block=True, timeout=0.1)
                if not self._tier0_inflight:
                    break
                continue
            self._tier0_scan_sources(
                batch,
                target_module=self._tier0_target_module(module_name, policy=policy),
            )
            files_scanned += len(batch)
            if module_name in self._module_to_files and policy == RESOLVE_CONFIDENT:
                break

        if (
            module_name not in self._module_to_files
            and policy == RESOLVE_RECOVERY
        ):
            remaining = [
                src
                for src in self._sources
                if src not in self._regex_scanned and src not in self._tier0_inflight
            ]
            if remaining:
                remaining = self._sort_files_by_resolve_rank(
                    remaining,
                    scope_anchor=scope_anchor,
                    module_name=module_name,
                    inst_leaf=inst_leaf,
                )
                self._tier0_scan_sources(
                    remaining[: pw_tier0_global_scan_max()],
                    target_module=self._tier0_target_module(module_name, policy=policy),
                )

        files = list(self._module_to_files.get(module_name, []))
        scope_pool = (
            self._scoped_pool_for_policy(scope_anchor, policy=policy)
            if scope_anchor
            else None
        )
        return self._sort_module_files(
            module_name,
            files,
            scope_anchor=scope_anchor,
            policy=policy,
            scope_pool=scope_pool,
            inst_leaf=inst_leaf,
        )

    def _parent_instance_edges(
        self,
        module_name: str,
        parent_ctx: Mapping[str, str],
    ) -> List[InstanceEdge]:
        """Tier-1 prescanned edges when available; else lazy fold via the index."""
        return self._index.instances_for_walk(module_name, parent_ctx)

    def _order_candidate_files(
        self,
        module_name: str,
        *,
        avoid_file: str = "",
        scope_anchor: str = "",
        policy: ResolvePolicy = RESOLVE_CONFIDENT,
        inst_leaf: str = "",
    ) -> List[str]:
        candidates = self._ensure_regex_candidates(
            module_name,
            scope_anchor=scope_anchor,
            policy=policy,
            inst_leaf=inst_leaf,
        )
        if not candidates:
            return []
        avoid = str(Path(avoid_file).resolve()) if avoid_file else ""
        if avoid:
            rest = [f for f in candidates if f != avoid]
            if avoid in candidates:
                return rest + [avoid]
            return rest
        return candidates

    def _recovery_pass1_candidates(
        self,
        module_name: str,
        *,
        pending: int,
        tried: Set[str],
        avoid_file: str,
        scope_anchor: str,
        trace_label: str,
    ) -> Optional[List[str]]:
        """
        Recovery pass-1 global tier0 expand.

        ``pending`` counts newly submitted tier0 scans only; ``_module_to_files``
        may already list candidates when ``pending == 0``. Returns untried paths
        to retry, or None to stop the outer pass loop.
        """
        if pending <= 0 and module_name not in self._module_to_files:
            return None
        if pending > 0:
            self._trace(
                f"pw-db tier0 expand policy=recovery {trace_label} "
                f"global +{pending} file(s) -> "
                f"{len(self._module_to_files.get(module_name, []))} candidate(s)"
            )
        candidates = self._order_candidate_files(
            module_name,
            avoid_file=avoid_file,
            scope_anchor=scope_anchor,
            policy=RESOLVE_RECOVERY,
        )
        untried = [f for f in candidates if f not in tried]
        if not untried:
            return None
        return untried

    def tier1_scan_file(self, path: str) -> Dict[str, ModuleRecord]:
        """Light preprocess + instance scan for one translation unit."""
        key = str(Path(path).resolve())
        with self._tier1_scan_lock:
            mem = self._validated_memory.get(key)
            if mem is not None:
                return mem

            self._set_phase("parsing", detail=Path(key).name)

            defs: Dict[str, str] = dict(self._defines)
            effective_digest = _defines_digest(defs)
            include_digest = self._include_closure_digest(key)

            disk = self._load_validated_sidecar(
                key,
                defines_digest=effective_digest,
                include_closure_digest=include_digest,
            )
            if disk is not None:
                self.cache_validated_hits += 1
                self._validated_memory[key] = disk
                self.files_validated += 1
                summary = ",".join(
                    f"{n}({len(r.instances)}inst)" for n, r in sorted(disk.items())
                )
                self._trace(f"pw-db tier1 cache {Path(key).name} -> {summary or '(none)'}")
                return disk

            from hierwalk.preprocess import apply_ifdef_filter, preprocess_file_for_index

            text = preprocess_file_for_index(
                Path(key),
                self._include_dirs,
                defs,
                set(),
                skip_path_patterns=self._skip,
            )
            # Tier-1 must honour filelist + in-file defines (ifdef instance names, gated modules).
            text = apply_ifdef_filter(text, defs)
            per_file = scan_preprocessed(text, key)
            out = {name: _record_lite(rec) for name, rec in per_file.items()}
            self._validated_memory[key] = out
            self._save_validated_sidecar(
                key,
                out,
                defines_digest=effective_digest,
                include_closure_digest=include_digest,
            )
            self.files_validated += 1
            for name in out:
                self._note_regex_modules(key, [name])
            summary = ",".join(
                f"{n}({len(r.instances)}inst)" for n, r in sorted(out.items())
            )
            self._trace(f"pw-db tier1 scan {Path(key).name} -> {summary or '(none)'}")
            return out

    def _invalidate_folded_edges_cache(
        self,
        *,
        mod_names: Optional[Set[str]] = None,
        file_path: str = "",
    ) -> None:
        if not self._folded_edges_cache:
            return
        if not mod_names and not file_path:
            self._folded_edges_cache.clear()
            return
        drop_mods = mod_names or set()
        file_key = str(Path(file_path).resolve()) if file_path else ""
        stale = [
            cache_key
            for cache_key in self._folded_edges_cache
            if cache_key[0] in drop_mods or (file_key and cache_key[2] == file_key)
        ]
        for cache_key in stale:
            del self._folded_edges_cache[cache_key]

    def _instance_edges_for_hit(
        self,
        hit: ModuleRecord,
        *,
        fpath: str,
        parent_ctx: Mapping[str, str],
    ) -> List[InstanceEdge]:
        """Tier-1 instance edges, including lazy generate-fold bodies."""
        from hierwalk.index import _ctx_key

        mod_name = hit.module_name
        key = str(Path(fpath).resolve())
        if not hit.needs_generate_fold:
            return list(hit.instances)
        idx_rec = self._index.get_module(mod_name)
        if (
            idx_rec is not None
            and not _is_placeholder_module(idx_rec)
            and idx_rec.file_path == key
        ):
            return self._index.instances_for(mod_name, parent_ctx, {})

        from hierwalk.preprocess import apply_ifdef_filter, preprocess_file_for_index

        defs: Dict[str, str] = dict(self._defines)
        text = preprocess_file_for_index(
            Path(key),
            self._include_dirs,
            defs,
            set(),
            skip_path_patterns=self._skip,
        )
        fold_ctx = dict(defs)
        fold_ctx.update(resolve_param_map(hit.raw_params, parent=parent_ctx))
        fold_cache_key = (mod_name, _ctx_key(fold_ctx), key)
        cached_edges = self._folded_edges_cache.get(fold_cache_key)
        if cached_edges is not None:
            return cached_edges

        text = apply_ifdef_filter(text, defs)
        _hdr, body = _module_header_body(text, mod_name)
        if not body:
            edges = list(hit.instances)
        else:
            edges = _scan_module_body(
                body,
                hit.raw_params,
                parent_ctx=parent_ctx,
                compile_defines=defs,
            )
        self._folded_edges_cache[fold_cache_key] = edges
        return edges

    def _edge_matches(
        self,
        edges: Sequence[InstanceEdge],
        inst_leaf: str,
        param_map: Mapping[str, str],
        *,
        parent_module: str = "",
        parent_ctx: Optional[Mapping[str, str]] = None,
    ) -> Optional[InstanceEdge]:
        """Match path segment to synthesizable instance names only (not module types)."""
        if not inst_leaf:
            return None
        if parent_module:
            indexed = self.lookup_inst_edge(
                parent_module,
                parent_ctx or {},
                inst_leaf,
            )
            if indexed is not None:
                return indexed
        if not edges:
            return None
        for edge in edges:
            if instance_edge_matches_leaf(edge, inst_leaf, param_map=param_map):
                return edge
        return None

    def _preprocessed_text_for_file(self, fpath: str) -> str:
        key = str(Path(fpath).resolve())
        defines_digest = self._defines_digest
        include_digest = self._include_closure_digest(key)
        mem_key = (key, defines_digest, include_digest)
        cached = self._preprocessed_text_cache.get(mem_key)
        if cached is not None:
            return cached
        disk = self._load_preprocessed_sidecar(
            key,
            defines_digest=defines_digest,
            include_closure_digest=include_digest,
        )
        if disk is not None:
            self._preprocessed_text_cache[mem_key] = disk
            self._trace(f"pw-db preprocess cache {Path(key).name}")
            return disk
        from hierwalk.lazy_scope import lazy_index_ifdef
        from hierwalk.preprocess import apply_ifdef_filter, preprocess_file_for_index

        defs = dict(self._defines)
        text = preprocess_file_for_index(
            Path(key),
            self._include_dirs,
            defs,
            set(),
            skip_path_patterns=self._skip,
        )
        if not lazy_index_ifdef():
            text = apply_ifdef_filter(text, defs)
        self._preprocessed_text_cache[mem_key] = text
        self._save_preprocessed_sidecar(
            key,
            text,
            defines_digest=defines_digest,
            include_closure_digest=include_digest,
        )
        return text

    def hint_edges_for_type_miss(
        self,
        parent_module: str,
        parent_ctx: Mapping[str, str],
        miss_leaf: str,
        fpath: str,
    ) -> List[InstanceEdge]:
        """Minimal parent scan to detect module-type-vs-instance miss hints."""
        try:
            text = self._preprocessed_text_for_file(fpath)
        except OSError:
            return []
        header, body = _module_header_body(text, parent_module)
        if not body:
            return []
        rec = self._index.get_module(parent_module)
        raw_params = dict(rec.raw_params) if rec and rec.raw_params else parse_param_pairs(header)
        pmap = resolve_param_map(raw_params, parent=parent_ctx)
        edge = find_instance_by_child_module(body, miss_leaf, param_map=pmap)
        return [edge] if edge is not None else []

    def _parent_module_body_text(
        self,
        fpath: str,
        parent_module: str,
    ) -> str:
        try:
            text = self._preprocessed_text_for_file(fpath)
        except OSError:
            return ""
        _header, body = _module_header_body(text, parent_module)
        return body

    def _inst_absent_from_parent_body(
        self,
        fpath: str,
        parent_module: str,
        inst_leaf: str,
    ) -> bool:
        """True when regex shows *inst_leaf* is not declared in *parent_module* body."""
        body = self._parent_module_body_text(fpath, parent_module)
        if not body:
            return False
        return not probe_inst_leaf_regex_fast(body, inst_leaf)

    def _selective_child_edge_lookup(
        self,
        parent_module: str,
        parent_ctx: Mapping[str, str],
        inst_leaf: str,
        fpath: str,
    ) -> Optional[InstanceEdge]:
        """Targeted parent-body scan for one instance (no full tier1 file walk)."""
        body = self._parent_module_body_text(fpath, parent_module)
        if not body:
            return None
        rec = self._index.get_module(parent_module)
        try:
            text = self._preprocessed_text_for_file(fpath)
        except OSError:
            text = ""
        header, _ = _module_header_body(text, parent_module)
        raw_params = dict(rec.raw_params) if rec and rec.raw_params else parse_param_pairs(header)
        pmap = resolve_param_map(raw_params, parent=parent_ctx)
        return find_hierarchy_instance(body, inst_leaf, param_map=pmap)

    def _apply_selective_edge_hit(
        self,
        fpath: str,
        parent_module: str,
        edge: InstanceEdge,
        *,
        parent_ctx: Mapping[str, str],
    ) -> None:
        key = str(Path(fpath).resolve())
        rec = self._index.get_module(parent_module)
        header, _body = _module_header_body(self._preprocessed_text_for_file(fpath), parent_module)
        raw_params = dict(rec.raw_params) if rec and rec.raw_params else parse_param_pairs(header)
        pmap = resolve_param_map(raw_params, parent=parent_ctx)
        instances: List[InstanceEdge] = list(rec.instances) if rec else []
        if not any(
            instance_edge_matches_leaf(e, edge.inst_name, param_map=pmap) for e in instances
        ):
            instances.append(edge)
        self._index.modules[parent_module] = ModuleRecord(
            module_name=parent_module,
            file_path=key,
            body="",
            raw_params=raw_params,
            instances=instances,
            needs_generate_fold=rec.needs_generate_fold if rec else False,
            is_blackbox=rec.is_blackbox if rec else False,
            is_interface=rec.is_interface if rec else False,
            stop_reason=rec.stop_reason if rec else "",
        )
        child = edge.child_module
        child_rec = self._index.get_module(child)
        if child_rec is None or not child_rec.file_path:
            self._index.modules[child] = ModuleRecord(
                module_name=child,
                file_path=key,
                body="",
                raw_params={},
                instances=[],
                needs_generate_fold=False,
            )
        self._index._rebuild_file_modules()
        self._note_regex_modules(key, [parent_module, child])
        self._prefer_file[parent_module] = key
        self._register_inst_edges(parent_module, parent_ctx, instances)
        self._snapshot_dirty = True

    def _ensure_module_light(self, module_name: str, fpath: str) -> bool:
        """Register one module from a file without tier1 scan of all modules in it."""
        try:
            text = self._preprocessed_text_for_file(fpath)
        except OSError:
            return False
        header, body = _module_header_body(text, module_name)
        if not header and not body:
            return False
        key = str(Path(fpath).resolve())
        prior = self._index.get_module(module_name)
        raw_params = parse_param_pairs(header)
        instances: List[InstanceEdge] = []
        if body:
            instances = scan_hierarchy_instances(
                body,
                param_map=resolve_param_map(raw_params),
            )
        elif prior is not None:
            instances = list(prior.instances)
        self._index.modules[module_name] = ModuleRecord(
            module_name=module_name,
            file_path=key,
            body="",
            raw_params=raw_params,
            instances=instances,
            needs_generate_fold=prior.needs_generate_fold if prior else False,
            is_blackbox=prior.is_blackbox if prior else False,
            is_interface=prior.is_interface if prior else False,
            stop_reason=prior.stop_reason if prior else "",
        )
        self._index._rebuild_file_modules()
        self._note_regex_modules(key, [module_name])
        self._prefer_file[module_name] = key
        self._register_inst_edges(module_name, {}, instances)
        self._snapshot_dirty = True
        return True

    def _warm_tier1_background(self, fpath: str) -> None:
        """Best-effort full tier1 DB warm in a background thread (not on walk hot path)."""
        key = str(Path(fpath).resolve())
        if key in self._validated_memory or key in self._tier1_warm_inflight:
            return
        self._tier1_warm_inflight.add(key)

        def _run() -> None:
            try:
                self.tier1_scan_file(key)
            except Exception as exc:  # noqa: BLE001 — background best-effort
                self._trace(f"pw-db tier1 warm-fail {Path(key).name}: {exc!r}")
            finally:
                self._tier1_warm_inflight.discard(key)

        threading.Thread(target=_run, name=f"pw-db-tier1-warm-{Path(key).name}", daemon=True).start()

    def _apply_file_modules(self, path: str, modules: Mapping[str, ModuleRecord]) -> None:
        key = str(Path(path).resolve())
        prior = list(self._file_to_modules.get(key, []))
        for name, rec in modules.items():
            self._index.modules[name] = ModuleRecord(
                module_name=name,
                file_path=key,
                body="",
                raw_params=dict(rec.raw_params),
                instances=list(rec.instances),
                needs_generate_fold=rec.needs_generate_fold,
                is_blackbox=rec.is_blackbox,
                is_interface=rec.is_interface,
                stop_reason=rec.stop_reason,
            )
        self._index._rebuild_file_modules()
        affected = set(prior) | set(modules)
        self._index.invalidate_instance_cache_for_modules(sorted(affected))
        self._index._rebuild_default_ctx()
        self._invalidate_folded_edges_cache(mod_names=affected, file_path=key)
        self._invalidate_inst_leaf_index(affected)
        for name, rec in modules.items():
            self._register_inst_edges(name, {}, rec.instances)
        self._snapshot_dirty = True

    def _tier1_file_defines_module(self, fpath: str, module_name: str) -> bool:
        """True when *fpath* contains a ``module`` definition for *module_name*."""
        if not fpath or not module_name:
            return False
        key = str(Path(fpath).resolve())
        rec = self._index.get_module(module_name)
        if (
            rec is not None
            and rec.file_path == key
            and not _is_placeholder_module(rec)
            and rec.instances
        ):
            return True
        try:
            text = self._preprocessed_text_for_file(fpath)
        except OSError:
            return False
        from hierwalk.inst_scan import iter_module_blocks

        for block in iter_module_blocks(text):
            if block["name"] == module_name:
                return True
        return False

    def _index_has_resolved_module(
        self,
        module_name: str,
        *,
        expect_inst: Optional[Tuple[str, str]] = None,
        parent_ctx: Optional[Mapping[str, str]] = None,
    ) -> bool:
        rec = self._index.get_module(module_name)
        if _is_placeholder_module(rec):
            return False
        if expect_inst is None:
            if rec is not None and rec.file_path:
                return self._tier1_file_defines_module(rec.file_path, module_name)
            return rec is not None
        parent_mod, inst_leaf = expect_inst
        if parent_mod != module_name or rec is None:
            return False
        pmap = resolve_param_map(rec.raw_params, parent=parent_ctx or {})
        return (
            self._edge_matches(
                self._parent_instance_edges(module_name, parent_ctx or {}),
                inst_leaf,
                pmap,
                parent_module=module_name,
                parent_ctx=parent_ctx or {},
            )
            is not None
        )

    def ensure_module_in_index(
        self,
        module_name: str,
        *,
        expect_inst: Optional[Tuple[str, str]] = None,
        parent_ctx: Optional[Mapping[str, str]] = None,
        scope_anchor: str = "",
        policy: ResolvePolicy = RESOLVE_CONFIDENT,
        target_path: str = "",
        reset_path: str = "",
    ) -> bool:
        """
        Load *module_name* into the shared index from the best candidate file.

        *policy=confident* searches direct child filelists only and defers on miss.
        *policy=recovery* may expand subtree/global pools to finish deferred work.
        """
        if self._is_ignored_module(module_name):
            return False

        if self._index_has_resolved_module(
            module_name,
            expect_inst=expect_inst,
            parent_ctx=parent_ctx,
        ):
            return True

        rec = self._index.get_module(module_name)
        avoid = ""
        if rec is not None and rec.file_path and not _is_placeholder_module(rec):
            avoid = str(Path(rec.file_path).resolve())
            if expect_inst is None and self._tier1_file_defines_module(avoid, module_name):
                if self._ensure_module_light(module_name, avoid):
                    self._trace(
                        f"pw-db   module light hit {module_name!r} via {Path(avoid).name}"
                    )
                    self._warm_tier1_background(avoid)
                    return True

        scoped_pool = (
            self._scoped_pool_for_policy(scope_anchor, policy=policy)
            if scope_anchor
            else None
        )
        candidates = self._order_candidate_files(
            module_name,
            avoid_file=avoid,
            scope_anchor=scope_anchor,
            policy=policy,
            inst_leaf=expect_inst[1] if expect_inst is not None else "",
        )
        scope_note = ""
        if scope_anchor and scoped_pool is not None:
            label = "child" if policy == RESOLVE_CONFIDENT else "subtree"
            scope_note = f" {label}_scope={len(scoped_pool)} src(s)"
        self._trace(
            f"pw-db resolve policy={policy} module={module_name} "
            f"candidates={len(candidates)}"
            + (f" expect_inst={expect_inst[1]!r}" if expect_inst else "")
            + scope_note
        )
        if policy == RESOLVE_CONFIDENT and scope_anchor and not candidates:
            self._enqueue_defer(
                DeferredResolve(
                    kind="module",
                    module_name=module_name,
                    scope_anchor=scope_anchor,
                    parent_ctx=_parent_ctx_key(parent_ctx),
                    expect_inst=expect_inst,
                    target_path=target_path,
                    reset_path=reset_path,
                    reason="no-tier0-in-child-fl",
                )
            )
            return False

        tried: Set[str] = set()
        max_pass = 2 if policy == RESOLVE_CONFIDENT else 3
        for pass_idx in range(max_pass):
            for fpath in candidates:
                if fpath in tried:
                    continue
                tried.add(fpath)
                modules = self.tier1_scan_file(fpath)
                hit = modules.get(module_name)
                if hit is None:
                    self._trace(
                        f"pw-db   miss {Path(fpath).name}: no module {module_name!r} after tier1"
                    )
                    continue
                if expect_inst is not None:
                    parent_mod, inst_leaf = expect_inst
                    if parent_mod == module_name:
                        pmap = resolve_param_map(hit.raw_params, parent=parent_ctx or {})
                        tier_edges = self._instance_edges_for_hit(
                            hit,
                            fpath=fpath,
                            parent_ctx=parent_ctx or {},
                        )
                        edge = self._edge_matches(
                            tier_edges,
                            inst_leaf,
                            pmap,
                            parent_module=module_name,
                            parent_ctx=parent_ctx or {},
                        )
                        if edge is None:
                            insts = ", ".join(e.inst_name for e in tier_edges[:12])
                            self._trace(
                                f"pw-db   miss {Path(fpath).name}: "
                                f"no inst {inst_leaf!r} (have: {insts or '(none)'})"
                            )
                            continue
                self._apply_file_modules(fpath, modules)
                self._prefer_file[module_name] = fpath
                self._trace(f"pw-db   hit {Path(fpath).name} for module {module_name!r}")
                if self._index.get_module(module_name) is not None:
                    self.write_module_index_snapshot()
                    return True
            if pass_idx == 0:
                if scoped_pool:
                    pending = self._scan_remaining_sources_tier0(
                        scoped_pool,
                        target_module=module_name,
                    )
                    if pending > 0:
                        expand_label = (
                            "child-fl" if policy == RESOLVE_CONFIDENT else "scoped"
                        )
                        self._trace(
                            f"pw-db tier0 expand policy={policy} module={module_name} "
                            f"{expand_label} +{pending} file(s) -> "
                            f"{len(self._module_to_files.get(module_name, []))} candidate(s)"
                        )
                        candidates = self._order_candidate_files(
                            module_name,
                            avoid_file=avoid,
                            scope_anchor=scope_anchor,
                            policy=policy,
                            inst_leaf=expect_inst[1] if expect_inst is not None else "",
                        )
                        continue
                continue
            if pass_idx == 1 and policy == RESOLVE_RECOVERY:
                pending = self._scan_remaining_sources_tier0(
                    None,
                    target_module=module_name,
                    scope_anchor=scope_anchor,
                )
                refreshed = self._recovery_pass1_candidates(
                    module_name,
                    pending=pending,
                    tried=tried,
                    avoid_file=avoid,
                    scope_anchor=scope_anchor,
                    trace_label=f"module={module_name}",
                )
                if refreshed is None:
                    break
                candidates = refreshed
                continue
            break

        if policy == RESOLVE_CONFIDENT and scope_anchor:
            self._enqueue_defer(
                DeferredResolve(
                    kind="module",
                    module_name=module_name,
                    scope_anchor=scope_anchor,
                    parent_ctx=_parent_ctx_key(parent_ctx),
                    expect_inst=expect_inst,
                    target_path=target_path,
                    reset_path=reset_path,
                    reason="tier1-miss-child-fl",
                )
            )
            return False

        if expect_inst is not None:
            return False
        return self._index.get_module(module_name) is not None and not _is_placeholder_module(
            self._index.get_module(module_name)
        )

    def resolve_child_edge(
        self,
        parent_module: str,
        parent_ctx: Mapping[str, str],
        inst_leaf: str,
        *,
        current_file: str = "",
        policy: ResolvePolicy = RESOLVE_CONFIDENT,
        target_path: str = "",
        reset_path: str = "",
    ) -> Optional[InstanceEdge]:
        """Tier-1 confirmed child edge, trying alternate decl files on miss."""
        avoid = current_file
        indexed = self.lookup_inst_edge(parent_module, parent_ctx, inst_leaf)
        if indexed is not None:
            return indexed
        if self._index_has_resolved_module(
            parent_module,
            expect_inst=(parent_module, inst_leaf),
            parent_ctx=parent_ctx,
        ):
            rec = self._index.get_module(parent_module)
            if rec is not None:
                pmap = resolve_param_map(rec.raw_params, parent=parent_ctx)
                edge = self._edge_matches(
                    self._parent_instance_edges(parent_module, parent_ctx),
                    inst_leaf,
                    pmap,
                    parent_module=parent_module,
                    parent_ctx=parent_ctx,
                )
                if edge is not None:
                    if rec.file_path:
                        self._prefer_file[parent_module] = str(
                            Path(rec.file_path).resolve()
                        )
                    return edge

        scope_anchor = avoid
        scoped_pool = (
            self._scoped_pool_for_policy(scope_anchor, policy=policy)
            if scope_anchor
            else None
        )
        candidates = self._order_candidate_files(
            parent_module,
            avoid_file=avoid,
            scope_anchor=scope_anchor,
            policy=policy,
            inst_leaf=inst_leaf,
        )
        scope_note = ""
        if scope_anchor and scoped_pool is not None:
            label = "child" if policy == RESOLVE_CONFIDENT else "subtree"
            scope_note = f" {label}_scope={len(scoped_pool)} src(s)"
        self._trace(
            f"pw-db edge policy={policy} {parent_module}.{inst_leaf} "
            f"candidates={len(candidates)}{scope_note}"
        )
        tried: Set[str] = set()
        if avoid:
            tried.add(avoid)
        if avoid:
            selective = self._selective_child_edge_lookup(
                parent_module,
                parent_ctx,
                inst_leaf,
                avoid,
            )
            if selective is not None:
                self._apply_selective_edge_hit(
                    avoid,
                    parent_module,
                    selective,
                    parent_ctx=parent_ctx,
                )
                self._trace(
                    f"pw-db   edge selective hit {parent_module}.{inst_leaf} "
                    f"via {Path(avoid).name} -> child {selective.child_module!r}"
                )
                self._warm_tier1_background(avoid)
                return selective
        if policy == RESOLVE_CONFIDENT and scope_anchor and not candidates:
            self._enqueue_defer(
                DeferredResolve(
                    kind="edge",
                    module_name=parent_module,
                    scope_anchor=scope_anchor,
                    inst_leaf=inst_leaf,
                    parent_ctx=_parent_ctx_key(parent_ctx),
                    expect_inst=(parent_module, inst_leaf),
                    target_path=target_path,
                    reset_path=reset_path,
                    reason="no-tier0-in-child-fl",
                )
            )
            return None

        max_pass = 2 if policy == RESOLVE_CONFIDENT else 3
        for pass_idx in range(max_pass):
            for fpath in candidates:
                if fpath in tried:
                    continue
                tried.add(fpath)
                selective = self._selective_child_edge_lookup(
                    parent_module,
                    parent_ctx,
                    inst_leaf,
                    fpath,
                )
                if selective is not None:
                    self._apply_selective_edge_hit(
                        fpath,
                        parent_module,
                        selective,
                        parent_ctx=parent_ctx,
                    )
                    self._trace(
                        f"pw-db   edge selective hit {parent_module}.{inst_leaf} "
                        f"via {Path(fpath).name} -> child {selective.child_module!r}"
                    )
                    self._warm_tier1_background(fpath)
                    return selective
                if self._inst_absent_from_parent_body(
                    fpath,
                    parent_module,
                    inst_leaf,
                ):
                    self._trace(
                        f"pw-db   edge skip tier1 {Path(fpath).name}: "
                        f"{parent_module}.{inst_leaf} absent (regex)"
                    )
                    continue
                modules = self.tier1_scan_file(fpath)
                hit = modules.get(parent_module)
                if hit is None:
                    self._trace(
                        f"pw-db   edge miss {Path(fpath).name}: "
                        f"no module {parent_module!r} after tier1"
                    )
                    continue
                pmap = resolve_param_map(hit.raw_params, parent=parent_ctx)
                tier_edges = self._instance_edges_for_hit(
                    hit,
                    fpath=fpath,
                    parent_ctx=parent_ctx,
                )
                edge = self._edge_matches(
                    tier_edges,
                    inst_leaf,
                    pmap,
                    parent_module=parent_module,
                    parent_ctx=parent_ctx,
                )
                if edge is not None:
                    self._apply_file_modules(fpath, modules)
                    self._prefer_file[parent_module] = fpath
                    self._trace(
                        f"pw-db   edge hit {parent_module}.{inst_leaf} "
                        f"via {Path(fpath).name} -> child {edge.child_module!r}"
                    )
                    self.write_module_index_snapshot()
                    return edge
                insts = ", ".join(
                    f"{e.inst_name}->{e.child_module}" for e in tier_edges[:12]
                )
                self._trace(
                    f"pw-db   edge miss {Path(fpath).name}: "
                    f"no inst {inst_leaf!r} (have: {insts or '(none)'})"
                )
            if pass_idx == 0:
                if scoped_pool:
                    pending = self._scan_remaining_sources_tier0(
                        scoped_pool,
                        target_module=parent_module,
                    )
                    if pending > 0:
                        expand_label = (
                            "child-fl" if policy == RESOLVE_CONFIDENT else "scoped"
                        )
                        self._trace(
                            f"pw-db tier0 expand policy={policy} edge "
                            f"{parent_module}.{inst_leaf} {expand_label} +{pending} file(s) -> "
                            f"{len(self._module_to_files.get(parent_module, []))} candidate(s)"
                        )
                        candidates = self._order_candidate_files(
                            parent_module,
                            avoid_file=avoid,
                            scope_anchor=scope_anchor,
                            policy=policy,
                            inst_leaf=inst_leaf,
                        )
                        continue
                continue
            if pass_idx == 1 and policy == RESOLVE_RECOVERY:
                pending = self._scan_remaining_sources_tier0(
                    None,
                    target_module=parent_module,
                    scope_anchor=scope_anchor,
                )
                refreshed = self._recovery_pass1_candidates(
                    parent_module,
                    pending=pending,
                    tried=tried,
                    avoid_file=avoid,
                    scope_anchor=scope_anchor,
                    trace_label=f"edge {parent_module}.{inst_leaf}",
                )
                if refreshed is None:
                    break
                candidates = refreshed
                continue
            break

        if policy == RESOLVE_CONFIDENT and scope_anchor:
            self._enqueue_defer(
                DeferredResolve(
                    kind="edge",
                    module_name=parent_module,
                    scope_anchor=scope_anchor,
                    inst_leaf=inst_leaf,
                    parent_ctx=_parent_ctx_key(parent_ctx),
                    expect_inst=(parent_module, inst_leaf),
                    target_path=target_path,
                    reset_path=reset_path,
                    reason="no-edge-in-child-fl",
                )
            )
        return None

    def find_module_decl_file(self, module_name: str) -> Optional[str]:
        files = self._ensure_regex_candidates(module_name)
        if not files:
            return None
        preferred = self._prefer_file.get(module_name)
        if preferred and preferred in files:
            return preferred
        return files[0]