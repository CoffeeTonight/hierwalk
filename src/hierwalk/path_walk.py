"""
Path-walk connect mode: build only RTL on endpoint instance chains + LCA subtrees.

Reuses :class:`DesignIndex`, :func:`elaborate` helpers, and
:class:`ConnectivitySession` without a full filelist index scan.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, TextIO, Tuple

from hierwalk.connect_endpoints import (
    DeclNetCache,
    _lca,
    _module_body_for_row,
    _net_base_declared_fast,
    _net_exists_in_module,
    _row_param_ctx_optional,
    is_module_local_signal_name,
    net_exists_in_module_fast,
    wire_tail_exists_fast,
)
from hierwalk.connect_scan import (
    _net_base_in_assign_regex_fast,
    _net_base_in_port_map_regex_fast,
)
from hierwalk.connect_request import ConnectivityRequest
from hierwalk.connectivity import ConnectivityBatchResult, ConnectivitySession
from hierwalk.filelist import FilelistResult
from hierwalk.ignore_path import (
    partition_sources,
    resolve_ignore_path_patterns,
    source_path_matches,
)
from hierwalk.index import DesignIndex, _ctx_key
from hierwalk.inst_scan import expand_inst_names, probe_inst_leaf_regex_fast
from hierwalk.lazy_scope import (
    endpoint_specs_from_checks,
    endpoint_specs_from_request,
    hierarchy_prefixes,
)
from hierwalk.library_scan import scan_library_modules
from hierwalk.models import FlatRow, InstanceEdge
from hierwalk.params import resolve_param_map
from hierwalk.hierarchy_log import (
    emit_path_walk_log,
    emit_path_walk_spine_log,
    format_path_walk_miss_line,
    format_signal_tail_line,
    open_path_walk_trace_log,
    path_walk_child_miss_reason,
    path_walk_inst_miss_reason,
    path_walk_trace_show_message,
)
from hierwalk.path_walk_db import (
    RESOLVE_CONFIDENT,
    RESOLVE_RECOVERY,
    DeferredResolve,
    PathWalkModuleDb,
    path_walk_db_cache_key,
)
from hierwalk.top_find import resolve_top_modules

_MODULE_DEF_RE = re.compile(
    r"^\s*(?:module|interface|program)\s+([A-Za-z_]\w*)",
    re.MULTILINE | re.IGNORECASE,
)

_CACHE_PARENT_LEAF = object()


def _cache_parent_trie_insert(trie: Dict[str, object], path: str) -> None:
    node: Dict[str, object] = trie
    for segment in path.split("."):
        child = node.get(segment)
        if not isinstance(child, dict):
            child = {}
            node[segment] = child
        node = child
    node[_CACHE_PARENT_LEAF] = True


def _cache_parent_trie_remove(trie: Dict[str, object], path: str) -> None:
    parts = path.split(".")
    stack: List[Tuple[Dict[str, object], str]] = []
    node: Dict[str, object] = trie
    for segment in parts:
        child = node.get(segment)
        if not isinstance(child, dict):
            return
        stack.append((node, segment))
        node = child
    node.pop(_CACHE_PARENT_LEAF, None)
    for parent, segment in reversed(stack):
        child = parent.get(segment)
        if isinstance(child, dict) and not child:
            del parent[segment]
        else:
            break


def _cache_parent_trie_collect(trie: Dict[str, object], prefix: str) -> List[str]:
    node: object = trie
    parts = prefix.split(".") if prefix else []
    for segment in parts:
        if not isinstance(node, dict):
            return []
        node = node.get(segment)
        if not isinstance(node, dict):
            return []
    if not isinstance(node, dict):
        return []

    out: List[str] = []

    def _walk(cur: Dict[str, object], path_parts: List[str]) -> None:
        if cur.get(_CACHE_PARENT_LEAF):
            out.append(".".join(path_parts))
        for key, child in cur.items():
            if key is _CACHE_PARENT_LEAF or not isinstance(child, dict):
                continue
            _walk(child, path_parts + [str(key)])

    _walk(node, parts)
    return out


@dataclass(frozen=True)
class _PendingWalkMiss:
    parent_path: str
    inst_leaf: str
    reason: str
    target_path: str


@dataclass
class PathWalkStats:
    modules_loaded: int = 0
    files_scanned: int = 0
    files_regex_scanned: int = 0
    files_validated: int = 0
    cache_regex_hits: int = 0
    cache_validated_hits: int = 0
    paths_walked: int = 0
    subtrees_expanded: int = 0
    checks_run: int = 0
    endpoint_specs_raw: int = 0
    endpoint_specs_unique: int = 0
    walk_target_calls: int = 0
    walk_target_skipped: int = 0
    walk_parallel_workers: int = 0
    walk_parallel_branches: int = 0
    recovery_passes: int = 0
    recovery_stalled: bool = False


class ModuleFileResolver:
    """Lazy module name → defining RTL file (filelist sources only)."""

    def __init__(
        self,
        sources: Sequence[str | Path],
        *,
        skip_path_patterns: Sequence[str] = (),
    ) -> None:
        self._sources = [str(Path(s).resolve()) for s in sources]
        self._skip = tuple(skip_path_patterns)
        self._module_to_file: Dict[str, str] = {}
        self._scanned_files: Set[str] = set()

    @property
    def files_scanned(self) -> int:
        return len(self._scanned_files)

    def remember(self, module_name: str, file_path: str) -> None:
        if module_name and file_path:
            self._module_to_file.setdefault(module_name, str(Path(file_path).resolve()))

    def seed_index(self, index: DesignIndex) -> None:
        for name, rec in index.modules.items():
            if rec.file_path:
                self.remember(name, rec.file_path)

    def _scan_file(self, path: str) -> None:
        if path in self._scanned_files:
            return
        self._scanned_files.add(path)
        if self._skip and source_path_matches(path, self._skip):
            return
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return
        for m in _MODULE_DEF_RE.finditer(text):
            self._module_to_file.setdefault(m.group(1), path)

    def find_file(self, module_name: str) -> Optional[str]:
        hit = self._module_to_file.get(module_name)
        if hit is not None:
            return hit
        for src in self._sources:
            if src in self._scanned_files:
                continue
            self._scan_file(src)
            hit = self._module_to_file.get(module_name)
            if hit is not None:
                return hit
        return self._module_to_file.get(module_name)


@dataclass
class PathWalkState:
    """Incremental hierarchy rows built by walking endpoint paths."""

    index: DesignIndex
    top: str
    mod_db: PathWalkModuleDb
    rows_by_path: Dict[str, FlatRow] = field(default_factory=dict)
    stats: PathWalkStats = field(default_factory=PathWalkStats)
    on_progress: Optional[Callable[[str], None]] = None
    trace_stream: Optional[TextIO] = None
    _trace_log: Optional[TextIO] = field(default=None, repr=False)
    _pending_misses: List[_PendingWalkMiss] = field(default_factory=list, repr=False)
    _resolve_step_cache: Dict[Tuple[str, str], Tuple[str, Optional[InstanceEdge]]] = (
        field(default_factory=dict, repr=False)
    )
    _expanded_inst_cache: Dict[Tuple[str, str], List[Tuple[str, InstanceEdge]]] = (
        field(default_factory=dict, repr=False)
    )
    _expanded_inst_by_module: Dict[str, Set[Tuple[str, str]]] = field(
        default_factory=dict, repr=False
    )
    _children_by_parent: Dict[str, Set[str]] = field(default_factory=dict, repr=False)
    _spec_targets: Dict[str, str] = field(default_factory=dict, repr=False)
    _walk_blocked_prefixes: Set[str] = field(default_factory=set, repr=False)
    _walk_blocked_skips: int = field(default=0, repr=False)
    _failed_edge_cache: Set[Tuple[str, str]] = field(default_factory=set, repr=False)
    _step_keys_by_parent: Dict[str, Set[Tuple[str, str]]] = field(
        default_factory=dict, repr=False
    )
    _step_keys_by_module: Dict[str, Set[Tuple[str, str]]] = field(
        default_factory=dict, repr=False
    )
    _failed_edge_by_parent: Dict[str, Set[Tuple[str, str]]] = field(
        default_factory=dict, repr=False
    )
    _failed_edge_by_module: Dict[str, Set[Tuple[str, str]]] = field(
        default_factory=dict, repr=False
    )
    _cached_parent_trie: Dict[str, object] = field(default_factory=dict, repr=False)
    _decl_net_cache: DeclNetCache = field(default_factory=dict, repr=False)
    _module_body_cache: Dict[str, str] = field(default_factory=dict, repr=False)
    _param_ctx_cache: Dict[str, Mapping[str, str]] = field(default_factory=dict, repr=False)
    _signal_tail_records: List[object] = field(default_factory=list, repr=False)
    _walk_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _trace_streams(self) -> List[TextIO]:
        out: List[TextIO] = []
        if self.trace_stream is not None and not self.trace_stream.closed:
            out.append(self.trace_stream)
        if self._trace_log is not None and not self._trace_log.closed:
            out.append(self._trace_log)
        return out

    def _walk_trace_enabled(self) -> bool:
        return bool(self._trace_streams()) or self.on_progress is not None

    def _emit_walk(self, message: str) -> None:
        if not message or not path_walk_trace_show_message(message):
            return
        streams = self._trace_streams()
        if streams:
            for stream in streams:
                emit_path_walk_log(message, stream=stream)
        elif self.on_progress is not None:
            self.on_progress(f"path-walk: {message}")

    def _emit_walk_spine(self, path: str, *, title: str) -> None:
        streams = self._trace_streams()
        if not streams:
            return
        for stream in streams:
            emit_path_walk_spine_log(
                path,
                self.rows_by_path,
                stream=stream,
                title=title,
            )

    def _emit_walk_node(self, path: str, *, action: str = "ok") -> None:
        row = self.rows_by_path.get(path)
        if row is None or not self._walk_trace_enabled():
            return
        message = f"{action} {path}  module={row.module}"
        if row.file:
            message += f"  rtl={row.file}"
        if row.via_filelist:
            message += f"  via_filelist={row.via_filelist}"
        if row.filelist_chain:
            message += f"  filelist_chain={row.filelist_chain}"
        if row.stop_reason:
            message += f"  stop={row.stop_reason}"
        self._emit_walk(message)

    def _clear_pending_misses(self, target_path: str) -> None:
        if not target_path:
            return
        self._pending_misses = [
            miss
            for miss in self._pending_misses
            if miss.target_path != target_path
        ]

    def _is_walk_blocked(self, path: str) -> bool:
        """True when an ancestor miss already blocked walking *path* or below."""
        if not path:
            return False
        for block in self._walk_blocked_prefixes:
            if path == block or path.startswith(block + "."):
                return True
        return False

    def _note_blocked_walk_skip(self, path: str) -> None:
        if path and self._is_walk_blocked(path):
            self._walk_blocked_skips += 1

    def _block_walk_under(self, parent_path: str, inst_leaf: str) -> str:
        """Record the first unresolved instance prefix; skip deeper walks."""
        block = f"{parent_path}.{inst_leaf}" if parent_path else inst_leaf
        self._walk_blocked_prefixes.add(block)
        return block

    def _unblock_walk_prefix(self, path: str) -> None:
        if not path or not self._walk_blocked_prefixes:
            return
        drop = [
            block
            for block in self._walk_blocked_prefixes
            if path == block or path.startswith(block + ".") or block.startswith(path + ".")
        ]
        for block in drop:
            self._walk_blocked_prefixes.discard(block)

    def _queue_walk_miss(
        self,
        parent_path: str,
        inst_leaf: str,
        *,
        reason: str,
        target_path: str,
    ) -> None:
        miss_key = (parent_path, inst_leaf)
        if any(
            m.parent_path == parent_path and m.inst_leaf == inst_leaf
            for m in self._pending_misses
        ):
            self._block_walk_under(parent_path, inst_leaf)
            return
        self._clear_pending_misses(target_path)
        self._block_walk_under(parent_path, inst_leaf)
        self._pending_misses.append(
            _PendingWalkMiss(
                parent_path=parent_path,
                inst_leaf=inst_leaf,
                reason=reason,
                target_path=target_path,
            )
        )

    def _emit_walk_miss(
        self,
        parent_path: str,
        inst_leaf: str,
        *,
        reason: str,
        target_path: str = "",
    ) -> None:
        if not self._walk_trace_enabled():
            return
        parent = self.rows_by_path.get(parent_path)
        if parent is None:
            self._emit_walk(
                f"miss inst={inst_leaf} under {parent_path} "
                f"(cause=no-parent; {reason})  "
                f"(no parent elaboration row)"
            )
            return
        self._emit_walk(
            format_path_walk_miss_line(
                parent_path,
                parent,
                inst_leaf,
                reason=reason,
            )
        )
        spine_end = parent_path
        spine_title = (
            f"walked (target {target_path} stopped)" if target_path else "walked"
        )
        if self._trace_streams():
            self._emit_walk_spine(spine_end, title=spine_title)
        elif target_path:
            self._emit_walk(
                f"walked spine -> {spine_end} (target {target_path} stopped)"
            )

    def flush_pending_misses(self) -> None:
        """Emit one miss per unresolved edge; skip duplicate deeper targets."""
        pending = list(self._pending_misses)
        self._pending_misses.clear()
        best: Dict[Tuple[str, str], _PendingWalkMiss] = {}
        for miss in pending:
            if miss.target_path in self.rows_by_path:
                continue
            resolved = _walk_target_from_spec(miss.target_path, self)
            if resolved and resolved in self.rows_by_path:
                continue
            key = (miss.parent_path, miss.inst_leaf)
            prior = best.get(key)
            if prior is None or len(miss.target_path) < len(prior.target_path):
                best[key] = miss
        for miss in best.values():
            skipped = sum(
                1
                for other in pending
                if other is not miss
                and other.parent_path == miss.parent_path
                and other.inst_leaf == miss.inst_leaf
            )
            reason = miss.reason
            blocked_skips = self._walk_blocked_skips
            if skipped or blocked_skips:
                total = skipped + blocked_skips
                reason += (
                    f"; skipped {total} deeper walk target(s) blocked by this miss"
                )
            self._walk_blocked_skips = 0
            self._emit_walk_miss(
                miss.parent_path,
                miss.inst_leaf,
                reason=reason,
                target_path=miss.target_path,
            )

    def _sync_db_stats(self) -> None:
        self.stats.files_regex_scanned = self.mod_db.files_regex_scanned
        self.stats.files_validated = self.mod_db.files_validated
        self.stats.cache_regex_hits = self.mod_db.cache_regex_hits
        self.stats.cache_validated_hits = self.mod_db.cache_validated_hits
        self.stats.files_scanned = (
            self.mod_db.files_regex_scanned + self.mod_db.files_validated
        )

    def rows(self) -> List[FlatRow]:
        return list(self.rows_by_path.values())

    @property
    def signal_tail_records(self) -> List[object]:
        """Path-walk port/wire/reg tail probes (for hierarchy artifacts)."""
        return self._signal_tail_records

    def _add_row(
        self,
        mod: str,
        path: str,
        depth: int,
        parent: Optional[str],
        *,
        inst_leaf: str,
        file_path: str,
        stop_reason: str,
        param_ctx: Mapping[str, str],
    ) -> None:
        if path in self.rows_by_path:
            return
        self.rows_by_path[path] = FlatRow(
            full_path=path,
            inst_leaf=inst_leaf,
            module=mod,
            depth=depth,
            parent_path=parent,
            file=file_path,
            stop_reason=stop_reason,
            via_filelist=self.index.filelist_for(file_path),
            filelist_chain=self.index.filelist_chain_for(file_path),
            param_ctx=dict(param_ctx),
            param_ctx_folded=True,
        )
        if parent:
            self._children_by_parent.setdefault(parent, set()).add(path)
        self._emit_walk_node(path)

    def ensure_root(self) -> None:
        if self.top in self.rows_by_path:
            return
        rec = self.index.get_module(self.top)
        if rec is None:
            raise ValueError(f"top module not in index: {self.top}")
        stop = self.index.module_stop_reason(self.top)
        self._add_row(
            self.top,
            self.top,
            0,
            None,
            inst_leaf=self.top,
            file_path=rec.file_path,
            stop_reason=stop,
            param_ctx=resolve_param_map(rec.raw_params),
        )

    def _track_cached_parent(self, parent_path: str) -> None:
        if parent_path:
            _cache_parent_trie_insert(self._cached_parent_trie, parent_path)

    def _untrack_cached_parent_if_empty(self, parent_path: str) -> None:
        if not parent_path:
            return
        if self._step_keys_by_parent.get(parent_path) or self._failed_edge_by_parent.get(
            parent_path
        ):
            return
        _cache_parent_trie_remove(self._cached_parent_trie, parent_path)

    def _remember_resolve_step(self, step_key: Tuple[str, str]) -> None:
        parent_path = step_key[0]
        self._track_cached_parent(parent_path)
        self._step_keys_by_parent.setdefault(parent_path, set()).add(step_key)
        row = self.rows_by_path.get(parent_path)
        if row is not None:
            self._step_keys_by_module.setdefault(row.module, set()).add(step_key)

    def _forget_resolve_step(self, step_key: Tuple[str, str]) -> None:
        parent_path = step_key[0]
        bucket = self._step_keys_by_parent.get(parent_path)
        if bucket is not None:
            bucket.discard(step_key)
            if not bucket:
                del self._step_keys_by_parent[parent_path]
        row = self.rows_by_path.get(parent_path)
        if row is not None:
            mod_bucket = self._step_keys_by_module.get(row.module)
            if mod_bucket is not None:
                mod_bucket.discard(step_key)
                if not mod_bucket:
                    del self._step_keys_by_module[row.module]
        self._untrack_cached_parent_if_empty(parent_path)

    def _set_resolve_step_cache(
        self,
        step_key: Tuple[str, str],
        out: Tuple[str, Optional[InstanceEdge]],
    ) -> None:
        if step_key not in self._resolve_step_cache:
            self._remember_resolve_step(step_key)
        self._resolve_step_cache[step_key] = out

    def _mark_failed_edge(self, parent_path: str, seg: str) -> None:
        key = (parent_path, seg)
        if key in self._failed_edge_cache:
            return
        self._track_cached_parent(parent_path)
        self._failed_edge_cache.add(key)
        self._failed_edge_by_parent.setdefault(parent_path, set()).add(key)
        row = self.rows_by_path.get(parent_path)
        if row is not None:
            self._failed_edge_by_module.setdefault(row.module, set()).add(key)

    def _clear_failed_edge(self, parent_path: str, seg: str) -> None:
        key = (parent_path, seg)
        if key not in self._failed_edge_cache:
            return
        self._failed_edge_cache.discard(key)
        bucket = self._failed_edge_by_parent.get(parent_path)
        if bucket is not None:
            bucket.discard(key)
            if not bucket:
                del self._failed_edge_by_parent[parent_path]
        row = self.rows_by_path.get(parent_path)
        if row is not None:
            mod_bucket = self._failed_edge_by_module.get(row.module)
            if mod_bucket is not None:
                mod_bucket.discard(key)
                if not mod_bucket:
                    del self._failed_edge_by_module[row.module]
        self._untrack_cached_parent_if_empty(parent_path)

    def _drop_resolve_steps_for_parents(self, parents: Sequence[str]) -> None:
        for parent_path in parents:
            for step_key in list(self._step_keys_by_parent.pop(parent_path, ())):
                self._resolve_step_cache.pop(step_key, None)
                row = self.rows_by_path.get(parent_path)
                if row is not None:
                    mod_bucket = self._step_keys_by_module.get(row.module)
                    if mod_bucket is not None:
                        mod_bucket.discard(step_key)
                        if not mod_bucket:
                            del self._step_keys_by_module[row.module]
            self._untrack_cached_parent_if_empty(parent_path)

    def _drop_failed_edges_for_parents(self, parents: Sequence[str]) -> None:
        for parent_path in parents:
            for edge_key in list(self._failed_edge_by_parent.pop(parent_path, ())):
                self._failed_edge_cache.discard(edge_key)
                row = self.rows_by_path.get(parent_path)
                if row is not None:
                    mod_bucket = self._failed_edge_by_module.get(row.module)
                    if mod_bucket is not None:
                        mod_bucket.discard(edge_key)
                        if not mod_bucket:
                            del self._failed_edge_by_module[row.module]
            self._untrack_cached_parent_if_empty(parent_path)

    def _invalidate_walk_caches(
        self,
        *,
        module_name: str = "",
        path_prefix: str = "",
    ) -> None:
        if path_prefix:
            parents = _cache_parent_trie_collect(
                self._cached_parent_trie,
                path_prefix,
            )
            self._drop_resolve_steps_for_parents(parents)
            self._drop_failed_edges_for_parents(parents)
            return
        if module_name:
            for step_key in list(self._step_keys_by_module.pop(module_name, ())):
                self._resolve_step_cache.pop(step_key, None)
                parent_path = step_key[0]
                bucket = self._step_keys_by_parent.get(parent_path)
                if bucket is not None:
                    bucket.discard(step_key)
                    if not bucket:
                        del self._step_keys_by_parent[parent_path]
                self._untrack_cached_parent_if_empty(parent_path)
            for edge_key in list(self._failed_edge_by_module.pop(module_name, ())):
                self._failed_edge_cache.discard(edge_key)
                parent_path = edge_key[0]
                bucket = self._failed_edge_by_parent.get(parent_path)
                if bucket is not None:
                    bucket.discard(edge_key)
                    if not bucket:
                        del self._failed_edge_by_parent[parent_path]
                self._untrack_cached_parent_if_empty(parent_path)
            for key in list(self._expanded_inst_by_module.pop(module_name, ())):
                self._expanded_inst_cache.pop(key, None)
            return
        self._resolve_step_cache.clear()
        self._step_keys_by_parent.clear()
        self._step_keys_by_module.clear()
        self._expanded_inst_cache.clear()
        self._expanded_inst_by_module.clear()
        self._failed_edge_cache.clear()
        self._failed_edge_by_parent.clear()
        self._failed_edge_by_module.clear()
        self._cached_parent_trie.clear()

    def _load_module(
        self,
        module_name: str,
        *,
        expect_inst: Optional[Tuple[str, str]] = None,
        parent_ctx: Optional[Mapping[str, str]] = None,
        scope_anchor: str = "",
        policy: str = RESOLVE_CONFIDENT,
        target_path: str = "",
        reset_path: str = "",
    ) -> bool:
        had = self.index.get_module(module_name)
        if not self.mod_db.ensure_module_in_index(
            module_name,
            expect_inst=expect_inst,
            parent_ctx=parent_ctx,
            scope_anchor=scope_anchor,
            policy=policy,
            target_path=target_path,
            reset_path=reset_path,
        ):
            self._sync_db_stats()
            self._emit_walk(
                f"pw-db load failed module={module_name!r} "
                f"{self.mod_db.format_status_line()}"
            )
            return False
        rec = self.index.get_module(module_name)
        if rec is None:
            self._sync_db_stats()
            return False
        if had is None or (had.file_path or "") != (rec.file_path or ""):
            self.stats.modules_loaded += 1
            self._invalidate_walk_caches(module_name=module_name)
        self._sync_db_stats()
        return True

    def _child_edge(
        self,
        parent_path: str,
        inst_leaf: str,
        *,
        target_path: str = "",
        policy: str = RESOLVE_CONFIDENT,
    ) -> Optional[InstanceEdge]:
        row = self.rows_by_path.get(parent_path)
        if row is None:
            return None
        edge = self.mod_db.resolve_child_edge(
            row.module,
            row.param_ctx,
            inst_leaf,
            current_file=row.file,
            policy=policy,
            target_path=target_path,
            reset_path=parent_path,
        )
        if edge is not None:
            self._invalidate_walk_caches(module_name=row.module)
        self._sync_db_stats()
        return edge

    def _attach_child(
        self,
        parent_path: str,
        inst_leaf: str,
        edge: InstanceEdge,
        *,
        policy: str = RESOLVE_CONFIDENT,
    ) -> Optional[str]:
        parent = self.rows_by_path.get(parent_path)
        if parent is None:
            return None
        child_path = f"{parent_path}.{inst_leaf}"
        if child_path in self.rows_by_path:
            return child_path
        if not self._load_module(
            edge.child_module,
            scope_anchor=parent.file,
            target_path=child_path,
            policy=policy,
            reset_path=parent_path,
        ):
            return None
        rec = self.index.get_module(edge.child_module)
        if rec is None:
            return None
        pmap = resolve_param_map(
            rec.raw_params,
            overrides=edge.param_overrides,
            parent=parent.param_ctx,
        )
        stop = self.index.module_stop_reason(edge.child_module)
        self._add_row(
            edge.child_module,
            child_path,
            parent.depth + 1,
            parent_path,
            inst_leaf=inst_leaf,
            file_path=rec.file_path,
            stop_reason=stop,
            param_ctx=pmap,
        )
        return child_path

    @staticmethod
    def _inst_leaf_prefix(remainder: str) -> str:
        """First dotted path segment of *remainder* (``c.d.e`` -> ``c``, ``c[0][1].d`` -> ``c[0][1]``)."""
        if not remainder:
            return ""
        return remainder.split(".", 1)[0]

    @staticmethod
    def _is_terminal_path_segment(remainder: str) -> bool:
        """True when *remainder* is one hierarchy step (wire/port/reg tail), not ``inst.child``."""
        text = (remainder or "").strip()
        return bool(text) and "." not in text

    @staticmethod
    def _is_target_terminal_tail(cur: str, remainder: str, path: str) -> bool:
        """True when *remainder* is the final spec segment (port/wire/reg), not a mid-hop inst."""
        if not PathWalkState._is_terminal_path_segment(remainder):
            return False
        return path == f"{cur}.{remainder}"

    @staticmethod
    def _is_folded_inst_prefix_miss(
        miss_leaf: str,
        edges: Sequence[InstanceEdge],
    ) -> bool:
        """
        True when *miss_leaf* is only a strict prefix of a folded instance name.

        Example: ``gen_loop[0]`` is not an instance, but ``gen_loop[0].u`` is.
        Prefix walks must not block the longer path on this pseudo-miss.
        """
        if not miss_leaf:
            return False
        prefix = miss_leaf + "."
        for edge in edges:
            if edge.inst_name.startswith(prefix):
                return True
        return False

    def _cached_module_body(self, row: FlatRow) -> str:
        key = str(row.file or row.module)
        hit = self._module_body_cache.get(key)
        if hit is not None:
            return hit
        body = _module_body_for_row(self.index, row)
        self._module_body_cache[key] = body
        return body

    def _cached_param_ctx(self, row: FlatRow) -> Mapping[str, str]:
        hit = self._param_ctx_cache.get(row.full_path)
        if hit is not None:
            return hit
        if row.param_ctx_folded or row.param_ctx:
            ctx = dict(row.param_ctx)
        else:
            from hierwalk.connect_endpoints import _port_param_ctx

            ctx = dict(_port_param_ctx(self.index, row, self.top))
        self._param_ctx_cache[row.full_path] = ctx
        return ctx

    def _rtl_line_count(self, row: FlatRow) -> int:
        body = self._cached_module_body(row)
        if body:
            return body.count("\n") + (1 if body.strip() else 0)
        return 0

    def _classify_signal_tail(
        self,
        parent_path: str,
        signal_name: str,
        row: FlatRow,
    ) -> Tuple[Optional[str], float]:
        """Return (kind, check_ms) where kind is port|wire|reg or None."""
        from hierwalk.connect_endpoints import classify_signal_tail_kind

        t0 = time.perf_counter()

        def _elapsed() -> float:
            return (time.perf_counter() - t0) * 1000.0

        body = self._cached_module_body(row)
        kind = classify_signal_tail_kind(
            self.index,
            row,
            signal_name,
            top=self.top,
            body=body,
        )
        return kind, _elapsed()

    def _emit_signal_tail(
        self,
        *,
        hit: bool,
        kind: str,
        parent_path: str,
        tail: str,
        target_path: str,
        row: FlatRow,
        check_ms: float,
    ) -> None:
        from hierwalk.connect_artifacts import SignalTailRecord

        target = target_path or (
            f"{parent_path}.{tail}" if parent_path and tail else parent_path or tail
        )
        self._signal_tail_records.append(
            SignalTailRecord(
                target_path=target,
                parent_path=parent_path,
                tail=tail,
                kind=kind,
                hit=hit,
                module=row.module,
            )
        )
        self._emit_walk(
            format_signal_tail_line(
                hit=hit,
                kind=kind,
                parent_path=parent_path,
                tail=tail,
                target_path=target_path,
                module=row.module,
                rtl_file=row.file,
                rtl_lines=self._rtl_line_count(row),
                check_ms=check_ms,
            )
        )

    def _resolve_signal_tail(
        self,
        parent_path: str,
        remainder: str,
        *,
        target_path: str = "",
    ) -> bool:
        """
        True when *remainder* names a port/net in the parent module, not a missing instance.

        Emits ``signal-tail hit|miss`` trace lines (with rtl line count + check_ms).
        """
        if not remainder:
            return False
        row = self.rows_by_path.get(parent_path)
        if row is None:
            return False

        kind, check_ms = self._classify_signal_tail(parent_path, remainder, row)
        if kind is not None:
            self._emit_signal_tail(
                hit=True,
                kind=kind,
                parent_path=parent_path,
                tail=remainder,
                target_path=target_path,
                row=row,
                check_ms=check_ms,
            )
            return True

        miss_leaf = self._inst_leaf_prefix(remainder)
        if (
            miss_leaf
            and miss_leaf != remainder
            and is_module_local_signal_name(remainder)
        ):
            prefix_kind, prefix_ms = self._classify_signal_tail(parent_path, miss_leaf, row)
            if prefix_kind is not None:
                self._emit_signal_tail(
                    hit=True,
                    kind=f"{prefix_kind}-prefix",
                    parent_path=parent_path,
                    tail=remainder,
                    target_path=target_path,
                    row=row,
                    check_ms=prefix_ms,
                )
                return True
            check_ms = max(check_ms, prefix_ms)

        self._emit_signal_tail(
            hit=False,
            kind="not-signal",
            parent_path=parent_path,
            tail=remainder,
            target_path=target_path,
            row=row,
            check_ms=check_ms,
        )
        return False

    def _is_signal_or_port_tail_miss(
        self,
        parent_path: str,
        remainder: str,
        *,
        target_path: str = "",
    ) -> bool:
        return self._resolve_signal_tail(
            parent_path,
            remainder,
            target_path=target_path,
        )

    def _longest_known_prefix(self, path: str) -> Tuple[str, str]:
        """Return the longest cached ancestor of *path* and the remaining suffix."""
        if path == self.top:
            return self.top, ""
        if not path.startswith(self.top + "."):
            return "", path
        parts = path.split(".")
        best_idx = 0
        for idx in range(1, len(parts)):
            candidate = ".".join(parts[: idx + 1])
            if candidate in self.rows_by_path:
                best_idx = idx
            else:
                break
        cur = ".".join(parts[: best_idx + 1])
        remainder = ".".join(parts[best_idx + 1 :]) if best_idx < len(parts) - 1 else ""
        return cur, remainder

    def _expanded_inst_pairs(self, row: FlatRow) -> List[Tuple[str, InstanceEdge]]:
        rec = self.index.get_module(row.module)
        if rec is None:
            return []
        pmap = resolve_param_map(rec.raw_params, parent=row.param_ctx)
        fold_ctx = dict(self.index._preprocess_defines)
        fold_ctx.update(pmap)
        cache_key = (row.module, _ctx_key(fold_ctx))
        cached = self._expanded_inst_cache.get(cache_key)
        if cached is not None:
            return cached
        edges = self.index.instances_for_walk(row.module, row.param_ctx)
        if not edges and row.param_ctx:
            edges = self.index.instances_for_walk(row.module, {})
        pairs: List[Tuple[str, InstanceEdge]] = []
        for edge in edges:
            for name in expand_inst_names(edge.inst_name, "", pmap):
                pairs.append((name, edge))
        self._expanded_inst_cache[cache_key] = pairs
        self._expanded_inst_by_module.setdefault(row.module, set()).add(cache_key)
        return pairs

    @staticmethod
    def _remainder_matches_inst(remainder: str, inst_name: str) -> bool:
        if remainder == inst_name:
            return True
        prefix = inst_name + "."
        return (
            remainder.startswith(prefix)
            or remainder.lower().startswith(prefix.lower())
        )

    def _resolve_child_step(
        self,
        parent_path: str,
        remainder: str,
        *,
        target_path: str = "",
        policy: str = RESOLVE_CONFIDENT,
    ) -> Tuple[str, Optional[InstanceEdge]]:
        """Match longest folded instance name at start of *remainder*."""
        step_key = (parent_path, remainder)
        cached = self._resolve_step_cache.get(step_key)
        if cached is not None:
            return cached
        row = self.rows_by_path.get(parent_path)
        if row is None or not remainder:
            out: Tuple[str, Optional[InstanceEdge]] = ("", None)
            self._set_resolve_step_cache(step_key, out)
            return out
        best_name, best_edge = self.mod_db.longest_inst_prefix_match(
            row.module,
            row.param_ctx or {},
            remainder,
        )
        if best_edge is not None:
            out = (best_name, best_edge)
            self._set_resolve_step_cache(step_key, out)
            return out
        best_name = ""
        best_edge = None
        for name, edge in self._expanded_inst_pairs(row):
            if self._remainder_matches_inst(remainder, name):
                if len(name) > len(best_name):
                    best_name = name
                    best_edge = edge
        if best_edge is not None:
            out = (best_name, best_edge)
            self._set_resolve_step_cache(step_key, out)
            return out
        seg = self._inst_leaf_prefix(remainder)
        if not seg:
            out = ("", None)
            self._set_resolve_step_cache(step_key, out)
            return out
        if (parent_path, seg) in self._failed_edge_cache:
            out = ("", None)
            self._set_resolve_step_cache(step_key, out)
            return out
        edge = self._child_edge(
            parent_path,
            seg,
            target_path=target_path,
            policy=policy,
        )
        if edge is not None:
            self._clear_failed_edge(parent_path, seg)
            out = (seg, edge)
            self._set_resolve_step_cache(step_key, out)
            return out
        self._mark_failed_edge(parent_path, seg)
        out = ("", None)
        self._set_resolve_step_cache(step_key, out)
        return out

    def _paths_under_prefix(self, prefix: str) -> List[str]:
        """Return instance paths at *prefix* and below (O(subtree) when indexed)."""
        if prefix in self.rows_by_path:
            drop = [prefix]
            queue = [prefix]
            while queue:
                parent = queue.pop()
                for child in self._children_by_parent.get(parent, ()):
                    drop.append(child)
                    queue.append(child)
            return drop
        parts = prefix.split(".")
        anchor = ""
        for i in range(len(parts) - 1, -1, -1):
            candidate = ".".join(parts[: i + 1])
            if candidate in self.rows_by_path:
                anchor = candidate
                break
        if anchor:
            drop: List[str] = []
            queue = [anchor]
            while queue:
                parent = queue.pop()
                for child in self._children_by_parent.get(parent, ()):
                    if child == prefix or child.startswith(prefix + "."):
                        drop.append(child)
                        queue.append(child)
            if drop:
                return drop
        return [
            path
            for path in self.rows_by_path
            if path == prefix or path.startswith(prefix + ".")
        ]

    def _reset_walk_subtree(self, prefix: str) -> None:
        """Drop cached rows/caches under *prefix* so recovery can re-walk."""
        if not prefix:
            return
        drop = self._paths_under_prefix(prefix)
        drop_modules = {
            self.rows_by_path[path].module
            for path in drop
            if path in self.rows_by_path
        }
        for path in drop:
            row = self.rows_by_path.get(path)
            if row is not None and row.parent_path:
                children = self._children_by_parent.get(row.parent_path)
                if children is not None:
                    children.discard(path)
                    if not children:
                        del self._children_by_parent[row.parent_path]
            self._children_by_parent.pop(path, None)
            del self.rows_by_path[path]
        self._invalidate_walk_caches(path_prefix=prefix)
        for mod in drop_modules:
            for key in list(self._expanded_inst_by_module.pop(mod, ())):
                self._expanded_inst_cache.pop(key, None)
        drop_blocks = [
            block
            for block in self._walk_blocked_prefixes
            if block == prefix
            or block.startswith(prefix + ".")
            or prefix.startswith(block + ".")
        ]
        for block in drop_blocks:
            self._walk_blocked_prefixes.discard(block)
        if self._pending_misses:
            self._pending_misses = [
                miss
                for miss in self._pending_misses
                if not (
                    miss.parent_path == prefix
                    or miss.parent_path.startswith(prefix + ".")
                    or miss.target_path == prefix
                    or (
                        miss.target_path
                        and miss.target_path.startswith(prefix + ".")
                    )
                )
            ]

    def _unblock_deferred_recovery(self, item: DeferredResolve) -> None:
        if item.reset_path:
            self._unblock_walk_prefix(item.reset_path)
        if item.kind == "edge":
            for block in self._edge_walk_block_candidates(item):
                self._unblock_walk_prefix(block)
        elif item.expect_inst:
            _, inst_leaf = item.expect_inst
            if item.reset_path and inst_leaf:
                self._unblock_walk_prefix(f"{item.reset_path}.{inst_leaf}")
        if item.target_path:
            parts = item.target_path.split(".")
            for i in range(2, len(parts) + 1):
                self._unblock_walk_prefix(".".join(parts[:i]))

    def _edge_walk_block_candidates(self, item: DeferredResolve) -> List[str]:
        """Scoped ``.{inst_leaf}`` paths that may be walk-blocked for one defer item."""
        inst_leaf = item.inst_leaf
        if not inst_leaf:
            return []
        candidates: List[str] = []
        if item.reset_path:
            candidates.append(f"{item.reset_path}.{inst_leaf}")
        if item.target_path:
            parts = item.target_path.split(".")
            for i in range(2, len(parts) + 1):
                prefix = ".".join(parts[:i])
                if prefix.endswith(f".{inst_leaf}"):
                    candidates.append(prefix)
        seen: Set[str] = set()
        out: List[str] = []
        for block in candidates:
            if block not in seen:
                seen.add(block)
                out.append(block)
        return out

    def _defer_edge_walk_blocked(self, item: DeferredResolve) -> bool:
        if item.kind != "edge":
            return False
        inst_leaf = item.inst_leaf
        if not inst_leaf:
            return False
        if item.scope_anchor and self.mod_db.should_retry_deferred_recovery(
            item.scope_anchor
        ):
            return False
        for block in self._edge_walk_block_candidates(item):
            if self._is_walk_blocked(block):
                return True
        return False

    def _recover_defer_item(self, item: DeferredResolve) -> str:
        """
        Try one deferred item under recovery policy.

        Returns ``ok``, ``skip_blocked`` (walk prefix blocked; retry later), or ``miss``.
        """
        if item.kind == "edge":
            if self._defer_edge_walk_blocked(item):
                return "skip_blocked"
            parent_ctx = dict(item.parent_ctx)
            edge = self.mod_db.resolve_child_edge(
                item.module_name,
                parent_ctx,
                item.inst_leaf,
                current_file=item.scope_anchor,
                policy=RESOLVE_RECOVERY,
                target_path=item.target_path,
                reset_path=item.reset_path,
            )
            if edge is not None:
                return "ok"
            return "miss"
        if item.kind == "module":
            parent_ctx = dict(item.parent_ctx)
            if self.mod_db.ensure_module_in_index(
                item.module_name,
                expect_inst=item.expect_inst,
                parent_ctx=parent_ctx,
                scope_anchor=item.scope_anchor,
                policy=RESOLVE_RECOVERY,
                target_path=item.target_path,
                reset_path=item.reset_path,
            ):
                return "ok"
            return "miss"
        return "miss"

    def _apply_recovery_results(
        self,
        recovered: Sequence[DeferredResolve],
        *,
        skip_targets: Optional[Set[str]] = None,
    ) -> int:
        """Reset/unblock recovered defers and re-walk their target paths."""
        if not recovered:
            return 0
        for prefix in _coalesce_reset_prefixes(
            {item.reset_path for item in recovered if item.reset_path}
        ):
            self._reset_walk_subtree(prefix)
        invalidated: Set[str] = set()
        for item in recovered:
            self._unblock_deferred_recovery(item)
            mod = item.module_name
            if mod and mod not in invalidated:
                self._invalidate_walk_caches(module_name=mod)
                invalidated.add(mod)
        targets = sorted(
            {item.target_path for item in recovered if item.target_path},
            key=len,
            reverse=True,
        )
        ok_paths = 0
        for path in targets:
            if skip_targets and path in skip_targets:
                continue
            if self.ensure_path(path, policy=RESOLVE_RECOVERY):
                ok_paths += 1
        return ok_paths

    def _partition_recovery_batch(
        self,
        items: Sequence[DeferredResolve],
    ) -> Tuple[List[DeferredResolve], List[DeferredResolve], List[DeferredResolve]]:
        recovered: List[DeferredResolve] = []
        skipped_blocked: List[DeferredResolve] = []
        missed: List[DeferredResolve] = []
        for item in items:
            result = self._recover_defer_item(item)
            if result == "ok":
                recovered.append(item)
            elif result == "skip_blocked":
                skipped_blocked.append(item)
            else:
                missed.append(item)
        return recovered, skipped_blocked, missed

    def run_recovery_pass(
        self,
        *,
        spec_targets: Optional[Mapping[str, str]] = None,
        plan_rewalk: bool = False,
    ) -> Tuple[int, int, List[DeferredResolve]]:
        """Retry deferred confident misses with recovery policy (subtree + global).

        Returns ``(recovered, requeued, recovered_items)``: defer items whose
        index/walk cache was updated, blocked items requeued for a later pass,
        and the recovered defer records (for targeted endpoint re-walk).
        """
        items = self.mod_db.take_defer_queue()
        if not items:
            return 0, 0, []
        self._emit_walk(f"recovery-pass start defer={len(items)}")
        recovered, skipped_blocked, missed = self._partition_recovery_batch(items)
        skip_targets = _recovery_skip_ensure_targets(
            recovered,
            spec_targets or {},
            _filter_specs_for_recovery_rewalk(self, spec_targets or {}, recovered),
        ) if plan_rewalk and spec_targets is not None and recovered else None
        ok_paths = self._apply_recovery_results(
            recovered,
            skip_targets=skip_targets,
        )

        still_blocked: List[DeferredResolve] = []
        if skipped_blocked:
            for item in skipped_blocked:
                if self.mod_db.should_retry_deferred_recovery(item.scope_anchor):
                    self._unblock_deferred_recovery(item)
            retry_recovered, still_blocked, retry_missed = self._partition_recovery_batch(
                skipped_blocked
            )
            merged_recovered = list(recovered) + retry_recovered
            retry_skip = _recovery_skip_ensure_targets(
                retry_recovered,
                spec_targets or {},
                _filter_specs_for_recovery_rewalk(
                    self,
                    spec_targets or {},
                    merged_recovered,
                ),
            ) if plan_rewalk and spec_targets is not None and retry_recovered else None
            ok_paths += self._apply_recovery_results(
                retry_recovered,
                skip_targets=retry_skip,
            )
            recovered = merged_recovered
            missed = list(missed) + retry_missed
            for item in still_blocked:
                self.mod_db.requeue_defer(item)

        seen_miss: Set[Tuple[str, str]] = set()
        persistent_misses: List[DeferredResolve] = []
        for item in missed:
            miss_key = (item.reset_path or "", item.target_path or "")
            if miss_key in seen_miss:
                self.mod_db.requeue_defer(item)
                continue
            seen_miss.add(miss_key)
            if item.kind != "edge":
                transient = bool(
                    item.scope_anchor
                    and self.mod_db.should_retry_deferred_recovery(item.scope_anchor)
                )
                if not transient:
                    persistent_misses.append(item)
            self.mod_db.requeue_defer(item)
        if persistent_misses:
            for prefix in _coalesce_reset_prefixes(
                {item.reset_path for item in persistent_misses if item.reset_path}
            ):
                self._reset_walk_subtree(prefix)
            miss_skip = skip_targets or set()
            for path in sorted(
                {item.target_path for item in persistent_misses if item.target_path},
                key=len,
                reverse=True,
            ):
                if path in miss_skip:
                    continue
                self.ensure_path(path, policy=RESOLVE_RECOVERY)

        targets = sorted({item.target_path for item in recovered if item.target_path})
        requeued = len(still_blocked) + len(missed)
        self._emit_walk(
            f"recovery-pass done recovered={len(recovered)} "
            f"requeued={requeued} "
            f"paths_ok={ok_paths}/{len(targets)} defer={len(items)}"
        )
        return len(recovered), requeued, recovered

    def ensure_path(
        self,
        instance_path: str,
        *,
        policy: str = RESOLVE_CONFIDENT,
    ) -> bool:
        """Walk ``top.u_child...`` loading RTL files on demand."""
        path = instance_path.strip()
        if not path:
            return False
        if path != self.top and not path.startswith(self.top + "."):
            return False
        if self._is_walk_blocked(path):
            self._note_blocked_walk_skip(path)
            return False
        self.mod_db._set_phase("searching", detail=path)
        if path in self.rows_by_path:
            self._clear_pending_misses(path)
            self._unblock_walk_prefix(path)
            return True
        if path == self.top:
            self.ensure_root()
            self._clear_pending_misses(path)
            self._unblock_walk_prefix(path)
            return True
        cur, remainder = self._longest_known_prefix(path)
        if not cur:
            return False
        if cur not in self.rows_by_path:
            self.ensure_root()
            cur = self.top
            remainder = path[len(self.top) + 1 :] if len(path) > len(self.top) else ""
        while remainder:
            nxt = f"{cur}.{remainder}"
            if nxt in self.rows_by_path:
                cur = nxt
                remainder = ""
                break
            inst_name, edge = self._resolve_child_step(
                cur,
                remainder,
                target_path=path,
                policy=policy,
            )
            if edge is None or not inst_name:
                row = self.rows_by_path.get(cur)
                parent_mod = row.module if row else "?"
                miss_leaf = self._inst_leaf_prefix(remainder)
                if any(
                    m.parent_path == cur and m.inst_leaf == miss_leaf
                    for m in self._pending_misses
                ):
                    self._note_blocked_walk_skip(path)
                    return False
                snap = self.mod_db.module_to_files_snapshot().get(parent_mod, [])
                parent_rec = self.index.get_module(parent_mod) if parent_mod else None
                edges: List[InstanceEdge] = []
                if parent_rec is not None:
                    ctx = row.param_ctx if row else {}
                    edges = self.index.instances_for_walk(parent_mod, ctx)
                    if not edges and ctx:
                        edges = self.index.instances_for_walk(parent_mod, {})
                    if not edges and row is not None and row.file:
                        edges = self.mod_db.hint_edges_for_type_miss(
                            parent_mod,
                            ctx,
                            miss_leaf,
                            row.file,
                        )
                if self._is_folded_inst_prefix_miss(miss_leaf, edges):
                    return False
                if self._is_target_terminal_tail(
                    cur, remainder, path
                ) and self._is_signal_or_port_tail_miss(
                    cur,
                    remainder,
                    target_path=path,
                ):
                    return False
                raw_source_has_inst = False
                if row is not None and miss_leaf:
                    body = self._cached_module_body(row)
                    if body:
                        raw_source_has_inst = probe_inst_leaf_regex_fast(
                            body, miss_leaf
                        )
                self._queue_walk_miss(
                    cur,
                    miss_leaf,
                    reason=path_walk_inst_miss_reason(
                        parent_mod=parent_mod,
                        parent_rec=parent_rec,
                        miss_leaf=miss_leaf,
                        edges=edges,
                        candidate_files=snap,
                        raw_source_has_inst=raw_source_has_inst,
                    ),
                    target_path=path,
                )
                self.mod_db.write_module_index_snapshot()
                return False
            attached = self._attach_child(cur, inst_name, edge, policy=policy)
            if attached is None:
                child_mod = edge.child_module or "?"
                self._queue_walk_miss(
                    cur,
                    inst_name,
                    reason=path_walk_child_miss_reason(
                        child_mod=child_mod,
                        child_rec=self.index.get_module(child_mod),
                    ),
                    target_path=path,
                )
                return False
            cur = attached
            remainder = remainder[len(inst_name) :].lstrip(".")
        self.stats.paths_walked += 1
        ok = path in self.rows_by_path
        if ok:
            self._clear_pending_misses(path)
            self._unblock_walk_prefix(path)
        return ok

    def _expand_subtree(self, inst_path: str) -> None:
        row = self.rows_by_path.get(inst_path)
        if row is None or row.stop_reason:
            return
        rec = self.index.get_module(row.module)
        if rec is None:
            return
        edges = self.index.instances_for(row.module, row.param_ctx, {})
        for edge in edges:
            leaves = expand_inst_names(
                edge.inst_name,
                "",
                resolve_param_map(rec.raw_params, parent=row.param_ctx),
            )
            for leaf in leaves:
                child_path = f"{inst_path}.{leaf}"
                if child_path in self.rows_by_path:
                    self._expand_subtree(child_path)
                    continue
                hit = self._attach_child(inst_path, leaf, edge)
                if hit is not None:
                    self._expand_subtree(hit)

    def ensure_lca_subtree(self, path_a: str, path_b: str) -> None:
        """Ensure both endpoint chains exist; avoid expanding unrelated siblings."""
        lca = _lca(path_a, path_b)
        if not lca:
            return
        if not self._is_walk_blocked(path_a):
            self.ensure_path(path_a)
        if not self._is_walk_blocked(path_b):
            self.ensure_path(path_b)
        if lca not in self.rows_by_path and not self._is_walk_blocked(lca):
            self.ensure_path(lca)


def _cached_walk_target_from_spec(spec: str, state: PathWalkState) -> str:
    """Return cached walk target for *spec*, resolving on first miss."""
    text = str(spec).strip()
    if not text:
        return ""
    hit = state._spec_targets.get(text)
    if hit is not None:
        return hit
    target = _walk_target_from_spec(text, state)
    state._spec_targets[text] = target
    return target


def _walk_target_from_spec(spec: str, state: PathWalkState) -> str:
    """
    Hierarchy path to walk for a connect/endpoint spec.

    Instance chain is walked first; only the final spec segment may truncate
    to a port/wire/reg tail in the parent module.
    """
    text = spec.strip()
    if not text:
        return ""
    lookup = state.rows_by_path
    if text in lookup:
        return text
    parts = text.split(".")
    last_idx = len(parts) - 1
    for i in range(last_idx, 0, -1):
        if i != last_idx:
            continue
        hier = ".".join(parts[:i])
        row = lookup.get(hier)
        if row is None:
            continue
        port = parts[-1]
        if net_exists_in_module_fast(
            state.index,
            row,
            port,
            top=state.top,
            cache=state._decl_net_cache,
            param_ctx=_row_param_ctx_optional(row),
            body=state._cached_module_body(row),
        ):
            return hier
    return text


def _longest_row_prefix(
    text: str,
    rows_by_path: Mapping[str, FlatRow],
) -> str:
    if text in rows_by_path:
        return text
    parts = text.split(".")
    for i in range(len(parts) - 1, 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in rows_by_path:
            return candidate
    return ""


def _inst_path_from_spec(
    spec: str,
    state: PathWalkState,
) -> str:
    """Resolved hierarchy prefix for error messages (may stop at first miss)."""
    text = spec.strip()
    if not text:
        return ""
    lookup = state.rows_by_path
    if text in lookup:
        return text
    if text != state.top and not text.startswith(state.top + "."):
        return text

    cur = _longest_row_prefix(text, lookup)
    if not cur:
        return text
    if text == cur:
        return cur

    remainder = text[len(cur) + 1 :]
    children_by_parent = state._children_by_parent
    while remainder:
        known_children = [
            path
            for path in children_by_parent.get(cur, ())
            if text.startswith(path)
        ]
        if known_children:
            cur = max(known_children, key=len)
            remainder = text[len(cur) + 1 :].lstrip(".")
            continue

        seg = remainder.split(".", 1)[0]
        if not seg:
            break
        nxt = f"{cur}.{seg}"
        if nxt not in lookup:
            row = lookup.get(cur)
            if row is not None and "." not in remainder:
                if net_exists_in_module_fast(
                    state.index,
                    row,
                    remainder,
                    top=state.top,
                    cache=state._decl_net_cache,
                    param_ctx=_row_param_ctx_optional(row),
                    body=state._cached_module_body(row),
                ):
                    return cur
            return nxt
        cur = nxt
        remainder = remainder[len(seg) :].lstrip(".")
    return cur


def _path_depth(path: str) -> int:
    return path.count(".") if path else 0


@dataclass
class _PathTrieNode:
    """Dotted hierarchy trie for endpoint specs (branch detection + ordered walk)."""

    segment: str = ""
    full_path: str = ""
    children: Dict[str, "_PathTrieNode"] = field(default_factory=dict)
    is_terminal: bool = False


def _path_trie_insert(root: _PathTrieNode, path: str, *, top: str) -> None:
    text = path.strip()
    if not text:
        return
    parts = text.split(".")
    if not parts:
        return
    if parts[0] == top:
        start = 1
        node = root
        cur = top
    else:
        start = 0
        node = root
        cur = ""
    for idx in range(start, len(parts)):
        seg = parts[idx]
        cur = seg if not cur else f"{cur}.{seg}"
        child = node.children.get(seg)
        if child is None:
            child = _PathTrieNode(segment=seg, full_path=cur)
            node.children[seg] = child
        node = child
    node.is_terminal = True


def _path_trie_from_specs(specs: Sequence[str], *, top: str) -> _PathTrieNode:
    root = _PathTrieNode(segment=top, full_path=top)
    for spec in specs:
        _path_trie_insert(root, str(spec).strip(), top=top)
    return root


def _path_trie_branch_points(root: _PathTrieNode) -> List[str]:
    """Instance paths where two or more child branches diverge (parallel walk candidates)."""
    out: List[str] = []
    stack: List[_PathTrieNode] = [root]
    while stack:
        node = stack.pop()
        if len(node.children) > 1 and node.full_path:
            out.append(node.full_path)
        stack.extend(node.children.values())
    return sorted(out, key=_path_depth)


def _path_trie_terminals(root: _PathTrieNode) -> List[str]:
    out: List[str] = []

    def visit(node: _PathTrieNode) -> None:
        if node.is_terminal and node.full_path:
            out.append(node.full_path)
        for child in sorted(node.children.values(), key=lambda n: n.segment):
            visit(child)

    visit(root)
    return out


def _sorted_unique_specs(specs: Sequence[str]) -> List[str]:
    seen: Set[str] = set()
    ordered: List[str] = []
    for raw in specs:
        text = str(raw).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return sorted(ordered, key=lambda p: (_path_depth(p), p))


def _resolve_path_walk_jobs(jobs: int, num_tasks: int) -> int:
    """Parallel hierarchy walk only when ``jobs`` is explicitly > 1."""
    if jobs <= 1:
        return 1
    return max(1, min(jobs, num_tasks))


def _path_trie_max_fanout(root: _PathTrieNode) -> int:
    best = 1
    stack: List[_PathTrieNode] = [root]
    while stack:
        node = stack.pop()
        if len(node.children) > 1:
            best = max(best, len(node.children))
        stack.extend(node.children.values())
    return best


def _filter_specs_under_prefix(specs: Sequence[str], prefix: str) -> List[str]:
    if not prefix:
        return [str(s).strip() for s in specs if str(s).strip()]
    dot = prefix + "."
    return [
        str(s).strip()
        for s in specs
        if str(s).strip() and (s == prefix or str(s).startswith(dot))
    ]


def _walk_one_endpoint_target(
    state: PathWalkState,
    spec: str,
    walked_targets: Set[str],
    spec_targets: Optional[Dict[str, str]] = None,
    *,
    policy: str = RESOLVE_CONFIDENT,
) -> None:
    text = str(spec).strip()
    if not text:
        return
    inst = state._spec_targets.get(text)
    if inst is None:
        inst = _walk_target_from_spec(text, state)
        state._spec_targets[text] = inst
    if not inst:
        return
    with state._walk_lock:
        if spec_targets is not None:
            spec_targets[text] = inst
        if inst in walked_targets:
            state.stats.walk_target_skipped += 1
            if state._is_walk_blocked(inst):
                state._note_blocked_walk_skip(inst)
            return
        walked_targets.add(inst)
        if state._is_walk_blocked(inst):
            state._note_blocked_walk_skip(inst)
            return
        state.stats.walk_target_calls += 1
        state.ensure_path(inst, policy=policy)


def _walk_specs_shallow_first(
    state: PathWalkState,
    specs: Sequence[str],
    walked_targets: Set[str],
    spec_targets: Optional[Dict[str, str]] = None,
    *,
    policy: str = RESOLVE_CONFIDENT,
) -> None:
    for spec in _sorted_unique_specs(specs):
        _walk_one_endpoint_target(
            state,
            spec,
            walked_targets,
            spec_targets=spec_targets,
            policy=policy,
        )


def _ensure_trie_prefix(
    state: PathWalkState,
    path: str,
    *,
    policy: str = RESOLVE_CONFIDENT,
) -> None:
    if not path or path == state.top:
        with state._walk_lock:
            state.ensure_root()
        return
    with state._walk_lock:
        state.ensure_path(path, policy=policy)


def _known_inst_leaves(state: PathWalkState, parent_path: str) -> Set[str]:
    row = state.rows_by_path.get(parent_path)
    if row is None:
        return set()
    rec = state.index.get_module(row.module)
    if rec is None:
        return set()
    edges = state.index.instances_for_walk(row.module, row.param_ctx)
    if not edges and row.param_ctx:
        edges = state.index.instances_for_walk(row.module, {})
    pmap = resolve_param_map(rec.raw_params, parent=row.param_ctx)
    leaves: Set[str] = set()
    for edge in edges:
        for leaf in expand_inst_names(edge.inst_name, "", pmap):
            leaves.add(leaf)
    return leaves


def _segment_matches_instance(
    segment: str,
    *,
    leaves: Set[str],
    edges: Sequence[InstanceEdge],
) -> bool:
    if segment in leaves:
        return True
    for leaf in leaves:
        if leaf.startswith(segment + ".") or leaf.startswith(segment + "["):
            return True
    for edge in edges:
        name = edge.inst_name
        if name == segment or name.startswith(segment + ".") or name.startswith(segment + "["):
            return True
    return False


def _is_instance_trie_branch(state: PathWalkState, node: _PathTrieNode) -> bool:
    """True when trie children are distinct instance names under an elaborated parent."""
    if len(node.children) <= 1:
        return False
    parent_path = node.full_path or state.top
    row = state.rows_by_path.get(parent_path)
    if row is None:
        return False
    rec = state.index.get_module(row.module)
    if rec is None:
        return False
    edges = state.index.instances_for_walk(row.module, row.param_ctx)
    if not edges and row.param_ctx:
        edges = state.index.instances_for_walk(row.module, {})
    if not edges:
        return False
    leaves = _known_inst_leaves(state, parent_path)
    return all(
        _segment_matches_instance(child.segment, leaves=leaves, edges=edges)
        for child in node.children.values()
    )


def _walk_trie_parallel(
    state: PathWalkState,
    node: _PathTrieNode,
    specs: Sequence[str],
    walked_targets: Set[str],
    *,
    workers: int,
    requested_jobs: int = 0,
    spec_targets: Optional[Dict[str, str]] = None,
    policy: str = RESOLVE_CONFIDENT,
) -> None:
    spec_list = [str(s).strip() for s in specs if str(s).strip()]
    if not spec_list:
        return

    children = sorted(node.children.values(), key=lambda child: child.segment)
    if not children:
        _walk_specs_shallow_first(
            state,
            spec_list,
            walked_targets,
            spec_targets=spec_targets,
            policy=policy,
        )
        return

    local_specs = [s for s in spec_list if s == node.full_path]
    if local_specs:
        _walk_specs_shallow_first(
            state,
            local_specs,
            walked_targets,
            spec_targets=spec_targets,
            policy=policy,
        )

    parallel_branch = (
        len(children) > 1
        and workers > 1
        and node.full_path
    )
    if parallel_branch:
        _ensure_trie_prefix(state, node.full_path, policy=policy)
        parallel_branch = _is_instance_trie_branch(state, node)

    if not parallel_branch:
        for child in children:
            child_specs = _filter_specs_under_prefix(spec_list, child.full_path)
            _walk_trie_parallel(
                state,
                child,
                child_specs,
                walked_targets,
                workers=workers,
                requested_jobs=requested_jobs,
                spec_targets=spec_targets,
                policy=policy,
            )
        return

    state.stats.walk_parallel_branches += 1
    branch_workers = min(workers, len(children))
    branch_labels = ",".join(child.segment for child in children)
    jobs_note = (
        f"requested_jobs={requested_jobs} workers={workers}"
        if requested_jobs > workers
        else f"jobs={workers}"
    )
    state._emit_walk(
        f"parallel fork at={node.full_path} {jobs_note} "
        f"pool={branch_workers} branches={branch_labels}"
    )

    def _child_task(slot: int, child: _PathTrieNode) -> None:
        child_specs = _filter_specs_under_prefix(spec_list, child.full_path)
        state._emit_walk(
            f"parallel worker j={slot}/{branch_workers} "
            f"branch={child.segment} from={node.full_path} "
            f"specs={len(child_specs)}"
        )
        try:
            _walk_trie_parallel(
                state,
                child,
                child_specs,
                walked_targets,
                workers=workers,
                requested_jobs=requested_jobs,
                spec_targets=spec_targets,
                policy=policy,
            )
        finally:
            state._emit_walk(
                f"parallel worker j={slot}/{branch_workers} "
                f"branch={child.segment} from={node.full_path} done"
            )

    with ThreadPoolExecutor(max_workers=branch_workers) as pool:
        futures = [
            pool.submit(_child_task, slot, child)
            for slot, child in enumerate(children, start=1)
        ]
        for fut in as_completed(futures):
            fut.result()


def _walk_endpoint_specs(
    state: PathWalkState,
    specs: Sequence[str],
    *,
    jobs: int = 0,
    spec_targets: Optional[Dict[str, str]] = None,
    policy: str = RESOLVE_CONFIDENT,
) -> None:
    """
    Walk endpoint specs with dedup + shallow-first order.

    Shared prefixes are built once via :meth:`PathWalkState.ensure_path`.
    At trie branch points (multiple child instance names), sibling subtrees
    are walked in parallel up to ``jobs`` workers.
    """
    raw = [str(s).strip() for s in specs if str(s).strip()]
    state.stats.endpoint_specs_raw += len(raw)
    unique = _sorted_unique_specs(raw)
    state.stats.endpoint_specs_unique += len(unique)
    if not unique:
        return

    walked_targets: Set[str] = set()
    root = _path_trie_from_specs(unique, top=state.top)
    fanout = _path_trie_max_fanout(root)
    workers = _resolve_path_walk_jobs(jobs, fanout)
    if workers > 1:
        state.stats.walk_parallel_workers = workers
        jobs_note = (
            f"requested_jobs={jobs} workers={workers}"
            if jobs > workers
            else f"jobs={workers}"
        )
        state._emit_walk(
            f"parallel walk enabled {jobs_note} fanout={fanout} "
            f"unique_specs={len(unique)}"
        )
    _walk_trie_parallel(
        state,
        root,
        unique,
        walked_targets,
        workers=workers,
        requested_jobs=jobs,
        spec_targets=spec_targets,
        policy=policy,
    )


def _sorted_prefixes(specs: Sequence[str]) -> List[str]:
    """Legacy: all dotted prefixes (prefer :func:`_walk_endpoint_specs`)."""
    prefixes = hierarchy_prefixes(specs)
    return sorted(prefixes, key=lambda p: (_path_depth(p), p))


@dataclass
class PathWalkSuiteSession:
    session_key: str
    index: DesignIndex
    mod_db: PathWalkModuleDb
    state: PathWalkState
    top_name: str
    trace_log_fh: Optional[TextIO] = field(default=None, repr=False)
    trace_log_path: Optional[Path] = None


_suite_session: Optional[PathWalkSuiteSession] = None


def path_walk_session_key(
    fl: FilelistResult,
    *,
    top: str,
    extra_defines: Mapping[str, str] | None = None,
    ignore_paths: Sequence[str] = (),
    ignore_path_files: Sequence[str] = (),
    ignore_modules: Sequence[str] = (),
    ignore_filelists: Sequence[str] = (),
    cache_dir: Optional[Path] = None,
    no_cache: bool = False,
    path_digests: Mapping[str, str] | None = None,
) -> str:
    defines = dict(fl.defines)
    defines.update(extra_defines or {})
    path_patterns, _, _ = resolve_ignore_path_patterns(
        ignore_paths,
        ignore_path_files=ignore_path_files,
        ignore_modules=ignore_modules,
        ignore_filelists=ignore_filelists,
    )
    sources = [str(p) for p in fl.source_files]
    db_key = path_walk_db_cache_key(
        sources,
        defines=defines,
        include_dirs=[str(p) for p in fl.include_dirs],
        skip_path_patterns=path_patterns,
        path_digests=path_digests,
    )
    hasher = hashlib.sha256()
    hasher.update(db_key.encode())
    hasher.update(top.encode())
    hasher.update(str(cache_dir or "").encode())
    hasher.update(b"1" if no_cache else b"0")
    return hasher.hexdigest()


def clear_path_walk_suite_session() -> None:
    global _suite_session
    if _suite_session is not None:
        trace_fh = _suite_session.trace_log_fh
        if trace_fh is not None and not trace_fh.closed:
            trace_fh.close()
        _suite_session.mod_db.shutdown_workers(wait=True)
        from hierwalk.manifest import clear_digest_scope
        from hierwalk.path_refine import clear_module_chunk_cache

        clear_digest_scope()
        clear_module_chunk_cache()
    _suite_session = None


def _acquire_path_walk_trace_log(
    log_path: Path,
    *,
    phase: str = "",
    reuse_suite_session: bool = False,
) -> tuple[Optional[TextIO], bool]:
    """Open or extend the path-walk trace log (append-only; suite steps share one handle)."""
    from hierwalk.hierarchy_log import (
        open_path_walk_trace_log,
        write_path_walk_trace_section,
    )

    if reuse_suite_session and _suite_session is not None:
        fh = _suite_session.trace_log_fh
        if (
            fh is not None
            and not fh.closed
            and _suite_session.trace_log_path == log_path
        ):
            write_path_walk_trace_section(fh, phase=phase)
            return fh, False
    fh = open_path_walk_trace_log(log_path, phase=phase)
    return fh, True


def acquire_path_walk_session(
    fl: FilelistResult,
    *,
    top: str = "",
    extra_defines: Mapping[str, str] | None = None,
    ignore_paths: Sequence[str] = (),
    ignore_path_files: Sequence[str] = (),
    ignore_modules: Sequence[str] = (),
    ignore_filelists: Sequence[str] = (),
    cache_dir: Optional[Path] = None,
    no_cache: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
    trace_stream: Optional[TextIO] = None,
    trace_log_fh: Optional[TextIO] = None,
    jobs: int = 0,
) -> PathWalkSuiteSession:
    global _suite_session
    from hierwalk.manifest import LazyPathDigests, set_digest_scope

    defines = dict(fl.defines)
    defines.update(extra_defines or {})
    sources = [str(Path(p).resolve()) for p in fl.source_files]
    path_digests = LazyPathDigests.for_paths(sources, jobs=jobs)
    session_key = path_walk_session_key(
        fl,
        top=top,
        extra_defines=extra_defines,
        ignore_paths=ignore_paths,
        ignore_path_files=ignore_path_files,
        ignore_modules=ignore_modules,
        ignore_filelists=ignore_filelists,
        cache_dir=cache_dir,
        no_cache=no_cache,
        path_digests=path_digests,
    )
    if _suite_session is not None and _suite_session.session_key == session_key:
        state = _suite_session.state
        state.on_progress = on_progress
        state.trace_stream = trace_stream
        if trace_log_fh is not None:
            state._trace_log = trace_log_fh
        _wire_db_trace_to_state(_suite_session.mod_db, state)
        _suite_session.mod_db._on_progress = on_progress
        return _suite_session

    if _suite_session is not None:
        clear_path_walk_suite_session()

    set_digest_scope(path_digests)
    index, mod_db = create_path_walk_index(
        fl,
        top,
        defines=defines,
        ignore_paths=ignore_paths,
        ignore_path_files=ignore_path_files,
        ignore_modules=ignore_modules,
        ignore_filelists=ignore_filelists,
        cache_dir=cache_dir,
        no_cache=no_cache,
        on_progress=on_progress,
        trace_stream=trace_stream,
        trace_log_fh=trace_log_fh,
        path_digests=path_digests,
        jobs=jobs,
    )
    tops = resolve_top_modules(index, top=top, filelist_tops=fl.top_modules)
    top_name = tops[0]
    state = PathWalkState(
        index=index,
        top=top_name,
        mod_db=mod_db,
        on_progress=on_progress,
        trace_stream=trace_stream,
        _trace_log=trace_log_fh,
    )
    _wire_db_trace_to_state(mod_db, state)
    state.ensure_root()
    _suite_session = PathWalkSuiteSession(
        session_key=session_key,
        index=index,
        mod_db=mod_db,
        state=state,
        top_name=top_name,
    )
    return _suite_session


def _coalesce_reset_prefixes(prefixes: Set[str]) -> List[str]:
    """Drop reset paths covered by a shorter ancestor in the same batch."""
    ordered = sorted(prefixes, key=len)
    roots: List[str] = []
    for prefix in ordered:
        if any(prefix == root or prefix.startswith(root + ".") for root in roots):
            continue
        roots.append(prefix)
    return roots


def _path_touches_prefixes(text: str, target: str, prefixes: Set[str]) -> bool:
    for prefix in prefixes:
        if (
            text == prefix
            or text.startswith(prefix + ".")
            or prefix.startswith(text + ".")
            or target == prefix
            or target.startswith(prefix + ".")
            or prefix.startswith(target + ".")
        ):
            return True
    return False


def _recovery_target_covered_by_rewalk(
    target_path: str,
    spec_targets: Mapping[str, str],
    affected_specs: Sequence[str],
) -> bool:
    """True when a pending endpoint re-walk will traverse *target_path*."""
    if not target_path:
        return False
    for spec in affected_specs:
        text = str(spec).strip()
        if not text:
            continue
        walk_target = spec_targets.get(text, text)
        for candidate in (walk_target, text):
            if candidate == target_path or candidate.startswith(target_path + "."):
                return True
    return False


def _recovery_skip_ensure_targets(
    recovered: Sequence[DeferredResolve],
    spec_targets: Mapping[str, str],
    affected_specs: Sequence[str],
) -> Set[str]:
    return {
        path
        for path in {item.target_path for item in recovered if item.target_path}
        if _recovery_target_covered_by_rewalk(path, spec_targets, affected_specs)
    }


def _recovery_walk_prefixes(recovered: Sequence[DeferredResolve]) -> Set[str]:
    """Minimal hierarchy prefixes that may need endpoint re-walk after recovery."""
    raw: Set[str] = set()
    for item in recovered:
        if item.reset_path:
            raw.add(item.reset_path)
        if item.target_path:
            raw.add(item.target_path)
    return set(_coalesce_reset_prefixes(raw))


def _filter_specs_for_recovery_rewalk(
    state: PathWalkState,
    spec_targets: Mapping[str, str],
    recovered: Sequence[DeferredResolve],
) -> List[str]:
    prefixes = _recovery_walk_prefixes(recovered)
    if not prefixes:
        return list(spec_targets)
    affected = [
        spec
        for spec, target in spec_targets.items()
        if _path_touches_prefixes(spec, target, prefixes)
    ]
    if affected:
        return affected
    return sorted({item.target_path for item in recovered if item.target_path})


def _walk_specs_with_recovery(
    state: PathWalkState,
    specs: Sequence[str],
    *,
    jobs: int = 0,
) -> None:
    """Walk endpoint specs; best-effort defer drain via recovery passes.

    Re-walks only endpoint specs touching recovered hierarchy prefixes when a pass
    recovers defer items (``recovered > 0``), not on requeue-only passes. Exits when
    a pass makes no progress or the defer queue depth does not shrink. Remaining
    defers are logged via ``recovery drain stalled``; path-walk/connect may proceed
    with partial hierarchy.
    """
    from hierwalk.perf import path_walk_recovery_pass_cap

    spec_targets = state._spec_targets
    _walk_endpoint_specs(state, specs, jobs=jobs, spec_targets=spec_targets)
    pass_cap = path_walk_recovery_pass_cap()
    while state.mod_db.defer_count() and state.stats.recovery_passes < pass_cap:
        defer_before = state.mod_db.defer_count()
        recovered, requeued, recovered_items = state.run_recovery_pass(
            spec_targets=spec_targets,
            plan_rewalk=True,
        )
        state.stats.recovery_passes += 1
        if recovered > 0:
            affected = _filter_specs_for_recovery_rewalk(
                state, spec_targets, recovered_items
            )
            _walk_endpoint_specs(
                state,
                affected,
                jobs=jobs,
                spec_targets=spec_targets,
                policy=RESOLVE_RECOVERY,
            )
            for spec in affected:
                text = str(spec).strip()
                if text:
                    spec_targets[text] = _walk_target_from_spec(text, state)
        defer_after = state.mod_db.defer_count()
        if recovered <= 0 and requeued <= 0:
            break
        if defer_after >= defer_before:
            break
    if state.mod_db.defer_count():
        state.stats.recovery_stalled = True
        state._emit_walk(
            f"recovery drain stalled defer_remaining={state.mod_db.defer_count()} "
            f"passes={state.stats.recovery_passes}/{pass_cap}"
        )


def _extend_path_walk_for_specs(
    state: PathWalkState,
    specs: Sequence[str],
    *,
    expand_subtrees: Sequence[str] = (),
    on_progress: Optional[Callable[[str], None]] = None,
    jobs: int = 0,
) -> None:
    spec_list = [str(s).strip() for s in specs if str(s).strip()]
    unique_n = len(_sorted_unique_specs(spec_list))
    if on_progress:
        jobs_note = "off" if jobs <= 1 else str(jobs)
        on_progress(
            f"path-walk: {len(spec_list)} endpoint spec(s)"
            f" ({unique_n} unique, trie walk, jobs={jobs_note})"
        )
    _walk_specs_with_recovery(state, spec_list, jobs=jobs)
    for subtree_root in expand_subtrees:
        root = str(subtree_root).strip()
        if not root:
            continue
        if state.ensure_path(root):
            state._expand_subtree(root)
            state.stats.subtrees_expanded += 1
    state.flush_pending_misses()


def _walk_hierarchy_for_check(
    state: PathWalkState,
    chk,
    *,
    jobs: int = 0,
    seen_lca: Optional[Set[Tuple[str, str]]] = None,
) -> None:
    """Per-check hierarchy walk + deduped LCA subtree (J-003)."""
    specs = endpoint_specs_from_checks([chk])
    if specs:
        _walk_specs_with_recovery(state, specs, jobs=jobs)
    a = _cached_walk_target_from_spec(chk.endpoint_a, state)
    b = _cached_walk_target_from_spec(chk.endpoint_b, state)
    key = (a, b)
    if seen_lca is not None:
        if key in seen_lca:
            return
        seen_lca.add(key)
    if state._is_walk_blocked(a) and state._is_walk_blocked(b):
        return
    state.ensure_lca_subtree(a, b)


def _extend_path_walk_connect(
    state: PathWalkState,
    request: ConnectivityRequest,
    *,
    on_progress: Optional[Callable[[str], None]] = None,
    jobs: int = 0,
) -> None:
    specs = endpoint_specs_from_request(request)
    unique_n = len(_sorted_unique_specs(specs))
    if on_progress:
        jobs_note = "off" if jobs <= 1 else str(jobs)
        on_progress(
            f"path-walk: {len(request.checks)} check(s), {len(specs)} endpoint spec(s)"
            f" ({unique_n} unique, per-check walk, jobs={jobs_note})"
        )
    seen_lca: Set[Tuple[str, str]] = set()
    for chk in request.checks:
        _walk_hierarchy_for_check(state, chk, jobs=jobs, seen_lca=seen_lca)
    state.flush_pending_misses()


def _init_path_walk_state_shell(
    index: DesignIndex,
    top: str,
    mod_db,
    *,
    on_progress: Optional[Callable[[str], None]] = None,
    trace_stream: Optional[TextIO] = None,
    trace_log_fh: Optional[TextIO] = None,
) -> PathWalkState:
    state = PathWalkState(
        index=index,
        top=top,
        mod_db=mod_db,
        on_progress=on_progress,
        trace_stream=trace_stream,
        _trace_log=trace_log_fh,
    )
    _wire_db_trace_to_state(mod_db, state)
    state.ensure_root()
    return state


def _pipeline_path_walk_text_conn(
    state: PathWalkState,
    request: ConnectivityRequest,
    conn_session: ConnectivitySession,
    *,
    hierarchy_jobs: int = 0,
    connect_jobs: int = 0,
    on_progress: Optional[Callable[[str], None]] = None,
) -> ConnectivityBatchResult:
    """Interleave per-check hierarchy walk with parallel text-COI workers (J-003/J-004)."""
    from hierwalk.connect_artifacts import prepare_text_connect_request
    from hierwalk.connectivity import _resolve_connect_jobs
    from hierwalk.verification_timing import record_connect_check

    text_request = prepare_text_connect_request(request)
    workers = _resolve_connect_jobs(connect_jobs, len(request.checks))
    use_trace = text_request.trace
    dedup_cache: Dict = {}
    dedup_stats = [0, 0]
    dedup_lock = threading.Lock()
    seen_lca: Set[Tuple[str, str]] = set()
    results: List = [None] * len(request.checks)

    state._emit_walk(
        f"connect-pipeline begin checks={len(request.checks)} "
        f"hierarchy_jobs={hierarchy_jobs} connect_jobs={workers}"
    )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for idx, chk in enumerate(request.checks):
            t_walk = time.perf_counter()
            _walk_hierarchy_for_check(
                state,
                chk,
                jobs=hierarchy_jobs,
                seen_lca=seen_lca,
            )
            walk_ms = (time.perf_counter() - t_walk) * 1000.0
            state._emit_walk(
                f"connect-pipeline hierarchy-ready check={chk.check_id or idx} "
                f"ms={walk_ms:.1f}"
            )
            t0 = time.perf_counter()

            def _run_check(
                _chk=chk,
                _idx=idx,
                _t0=t0,
            ):
                conn_session.resolve_param_dims = False
                result = conn_session.text_check_entry(
                    _chk,
                    trace=use_trace,
                    dedup_cache=dedup_cache,
                    dedup_stats=dedup_stats,
                    dedup_lock=dedup_lock,
                )
                record_connect_check(
                    check_id=_chk.check_id,
                    endpoint_a=str(_chk.endpoint_a),
                    endpoint_b=str(_chk.endpoint_b),
                    elapsed_sec=time.perf_counter() - _t0,
                )
                return _idx, result

            futures[pool.submit(_run_check)] = idx
        for fut in as_completed(futures):
            idx, result = fut.result()
            results[idx] = result

    state.flush_pending_misses()
    leaves, unique = dedup_stats[0], dedup_stats[1]
    if on_progress is not None and leaves > unique:
        on_progress(
            f"connect: text-coi dedup leaves={leaves} unique={unique} "
            f"saved={leaves - unique}"
        )
    state._emit_walk(
        f"connect-pipeline done checks={len(results)} workers={workers}"
    )
    return ConnectivityBatchResult(
        results=tuple(results),
        modules_cached=conn_session.modules_cached,
        text_coi_leaves=leaves,
        text_coi_unique=unique,
    )


def _path_walk_trace_emit(
    message: str,
    *,
    trace_stream: Optional[TextIO] = None,
    trace_log_fh: Optional[TextIO] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> None:
    if not message or not path_walk_trace_show_message(message):
        return
    streams: List[TextIO] = []
    if trace_stream is not None:
        streams.append(trace_stream)
    if trace_log_fh is not None:
        streams.append(trace_log_fh)
    if streams:
        for stream in streams:
            emit_path_walk_log(message, stream=stream)
    elif on_progress is not None:
        on_progress(f"path-walk: {message}")


def _wire_db_trace_to_state(mod_db: PathWalkModuleDb, state: PathWalkState) -> None:
    mod_db._on_trace = state._emit_walk


def build_path_walk_state_from_specs(
    index: DesignIndex,
    top: str,
    specs: Sequence[str],
    mod_db: PathWalkModuleDb,
    *,
    expand_subtrees: Sequence[str] = (),
    on_progress: Optional[Callable[[str], None]] = None,
    trace_stream: Optional[TextIO] = None,
    trace_log_path: Optional[Path] = None,
    trace_log_fh: Optional[TextIO] = None,
    close_trace_log: bool = True,
    jobs: int = 0,
) -> PathWalkState:
    """Walk endpoint specs; optionally expand instance subtrees for cone / inst-trace."""
    opened_log = False
    if trace_log_fh is None and trace_log_path is not None:
        trace_log_fh = open_path_walk_trace_log(trace_log_path)
        opened_log = True
    try:
        state = PathWalkState(
            index=index,
            top=top,
            mod_db=mod_db,
            on_progress=on_progress,
            trace_stream=trace_stream,
            _trace_log=trace_log_fh,
        )
        _wire_db_trace_to_state(mod_db, state)
        state.ensure_root()
        _extend_path_walk_for_specs(
            state,
            specs,
            expand_subtrees=expand_subtrees,
            on_progress=on_progress,
            jobs=jobs,
        )
        return state
    finally:
        if opened_log and close_trace_log and trace_log_fh is not None:
            trace_log_fh.close()


def build_path_walk_state(
    index: DesignIndex,
    top: str,
    request: ConnectivityRequest,
    mod_db: PathWalkModuleDb,
    *,
    on_progress: Optional[Callable[[str], None]] = None,
    trace_stream: Optional[TextIO] = None,
    trace_log_path: Optional[Path] = None,
    trace_log_fh: Optional[TextIO] = None,
    close_trace_log: bool = True,
    jobs: int = 0,
) -> PathWalkState:
    opened_log = False
    if trace_log_fh is None and trace_log_path is not None:
        trace_log_fh = open_path_walk_trace_log(trace_log_path)
        opened_log = True
    try:
        state = PathWalkState(
            index=index,
            top=top,
            mod_db=mod_db,
            on_progress=on_progress,
            trace_stream=trace_stream,
            _trace_log=trace_log_fh,
        )
        _wire_db_trace_to_state(mod_db, state)
        state.ensure_root()
        _extend_path_walk_connect(state, request, on_progress=on_progress, jobs=jobs)
        return state
    finally:
        if opened_log and close_trace_log and trace_log_fh is not None:
            trace_log_fh.close()


def run_path_walk_index(
    fl: FilelistResult,
    specs: Sequence[str],
    *,
    top: str = "",
    extra_defines: Mapping[str, str] | None = None,
    expand_subtrees: Sequence[str] = (),
    ignore_paths: Sequence[str] = (),
    ignore_path_files: Sequence[str] = (),
    ignore_modules: Sequence[str] = (),
    ignore_filelists: Sequence[str] = (),
    cache_dir: Optional[Path] = None,
    no_cache: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
    trace_stream: Optional[TextIO] = None,
    trace_log_path: Optional[Path] = None,
    reuse_suite_session: bool = False,
    jobs: int = 0,
) -> Tuple[DesignIndex, PathWalkState, str]:
    """On-demand index + hierarchy rows for arbitrary endpoint specs."""
    trace_log_fh: Optional[TextIO] = None
    opened_log = False
    if trace_log_path is not None:
        trace_log_fh, opened_log = _acquire_path_walk_trace_log(
            trace_log_path,
            reuse_suite_session=reuse_suite_session,
        )
    try:
        if reuse_suite_session:
            session = acquire_path_walk_session(
                fl,
                top=top,
                extra_defines=extra_defines,
                ignore_paths=ignore_paths,
                ignore_path_files=ignore_path_files,
                ignore_modules=ignore_modules,
                ignore_filelists=ignore_filelists,
                cache_dir=cache_dir,
                no_cache=no_cache,
                on_progress=on_progress,
                trace_stream=trace_stream,
                trace_log_fh=trace_log_fh,
                jobs=jobs,
            )
            state = session.state
            _extend_path_walk_for_specs(
                state,
                specs,
                expand_subtrees=expand_subtrees,
                on_progress=on_progress,
                jobs=jobs,
            )
            index = session.index
            top_name = session.top_name
        else:
            defines = dict(fl.defines)
            defines.update(extra_defines or {})
            index, mod_db = create_path_walk_index(
                fl,
                top,
                defines=defines,
                ignore_paths=ignore_paths,
                ignore_path_files=ignore_path_files,
                ignore_modules=ignore_modules,
                ignore_filelists=ignore_filelists,
                cache_dir=cache_dir,
                no_cache=no_cache,
                on_progress=on_progress,
                trace_stream=trace_stream,
                trace_log_fh=trace_log_fh,
                jobs=jobs,
            )
            tops = resolve_top_modules(index, top=top, filelist_tops=fl.top_modules)
            top_name = tops[0]
            state = build_path_walk_state_from_specs(
                index,
                top_name,
                specs,
                mod_db,
                on_progress=on_progress,
                trace_stream=trace_stream,
                trace_log_fh=trace_log_fh,
                close_trace_log=False,
                jobs=jobs,
            )
        extra_roots = list(expand_subtrees)
        for spec in specs:
            inst = _cached_walk_target_from_spec(spec, state)
            if inst and inst != top_name and inst not in extra_roots:
                extra_roots.append(inst)
        for subtree_root in extra_roots:
            root = str(subtree_root).strip()
            if not root:
                continue
            if state.ensure_path(root):
                state._expand_subtree(root)
                state.stats.subtrees_expanded += 1

        state._sync_db_stats()
        if on_progress:
            on_progress(
                f"path-walk: {len(state.rows_by_path)} instance row(s), "
                f"{state.stats.modules_loaded} module(s) loaded, "
                f"tier0={state.stats.files_regex_scanned} tier1={state.stats.files_validated} "
                f"cache={state.stats.cache_regex_hits}+{state.stats.cache_validated_hits}"
            )
        if state.mod_db.defer_count() and not state.stats.recovery_stalled:
            state.run_recovery_pass()
        _drain_path_walk_workers(state.mod_db)
        return index, state, top_name
    finally:
        if trace_log_fh is not None:
            if reuse_suite_session:
                if _suite_session is not None:
                    _suite_session.trace_log_fh = trace_log_fh
                    _suite_session.trace_log_path = trace_log_path
            elif opened_log:
                trace_log_fh.close()


_path_walk_phase_emit: Optional[Callable[[str], None]] = None


def bind_path_walk_phase_emit(
    emit: Optional[Callable[[str], None]],
) -> None:
    """Register walk trace sink for connect-COI / comb-build phases."""
    global _path_walk_phase_emit
    _path_walk_phase_emit = emit


def _reset_specs_for_logical_rewalk(
    state: PathWalkState,
    specs: Sequence[str],
) -> None:
    """Drop cached text-phase walk targets so logical pass re-walks with recovery."""
    for spec in specs:
        text = str(spec).strip()
        if not text:
            continue
        state._spec_targets.pop(text, None)
        inst = _walk_target_from_spec(text, state)
        if not inst:
            continue
        state._unblock_walk_prefix(inst)
        for prefix in hierarchy_prefixes([inst]):
            state._unblock_walk_prefix(prefix)


def _finalize_walk_endpoint_targets(
    state: PathWalkState,
    specs: Sequence[str],
    *,
    log_label: str = "connect",
) -> None:
    """Last-chance hierarchy walk so verification sees recovery-resolved paths."""
    targets: List[str] = []
    seen: Set[str] = set()
    for spec in specs:
        text = str(spec).strip()
        if not text:
            continue
        state._spec_targets.pop(text, None)
        inst = _walk_target_from_spec(text, state)
        if inst and inst not in seen:
            seen.add(inst)
            targets.append(inst)
    if not targets:
        return
    for target in sorted(targets, key=len):
        state._unblock_walk_prefix(target)
        for prefix in hierarchy_prefixes([target]):
            state._unblock_walk_prefix(prefix)
        if target in state.rows_by_path:
            continue
        state.ensure_path(target, policy=RESOLVE_RECOVERY)
    still_missing = [t for t in targets if t not in state.rows_by_path]
    if still_missing:
        state._emit_walk(
            f"walk finalize-before-{log_label} "
            f"missing={len(still_missing)} "
            f"sample={still_missing[0]!r}"
        )


def _drain_deferred_recovery_passes(state: PathWalkState) -> None:
    """Run recovery passes until defer queue stalls or makes no progress."""
    from hierwalk.perf import path_walk_recovery_pass_cap

    pass_cap = path_walk_recovery_pass_cap()
    while state.mod_db.defer_count() and state.stats.recovery_passes < pass_cap:
        defer_before = state.mod_db.defer_count()
        recovered, requeued, _ = state.run_recovery_pass(
            spec_targets=state._spec_targets,
            plan_rewalk=True,
        )
        state.stats.recovery_passes += 1
        defer_after = state.mod_db.defer_count()
        if recovered <= 0 and requeued <= 0:
            break
        if defer_after >= defer_before:
            break
    if state.mod_db.defer_count():
        state.stats.recovery_stalled = True


def finalize_logical_walk_before_connect(
    state: PathWalkState,
    request: ConnectivityRequest,
) -> None:
    """Deferred walk refinement for logical-conn: recovery, drain, endpoint re-walk."""
    if state.mod_db.defer_count() and not state.stats.recovery_stalled:
        _drain_deferred_recovery_passes(state)
    _drain_path_walk_workers(state.mod_db)
    _finalize_walk_endpoint_targets(
        state,
        endpoint_specs_from_request(request),
        log_label="connect",
    )


def sync_activation_to_walk_rows(
    mod_db: PathWalkModuleDb,
    state: PathWalkState,
) -> None:
    """Copy ifdef activation audit results onto walked hierarchy rows."""
    records = getattr(mod_db, "_mapped_inst_records", None)
    if not records:
        return
    for rec in records.values():
        for _path, row in state.rows_by_path.items():
            if row.inst_leaf != rec.inst_leaf:
                continue
            parent = state.rows_by_path.get(row.parent_path or "")
            if parent is None or parent.module != rec.parent_module:
                continue
            row.activation = rec.activation
            if rec.activation_detail:
                if row.walk_note:
                    row.walk_note = f"{row.walk_note}; {rec.activation_detail}"
                else:
                    row.walk_note = rec.activation_detail
            if rec.activation == "active" and row.refine_status == "provisional":
                row.refine_status = "confirmed"
            elif rec.activation == "inactive":
                row.refine_status = "inactive_ifdef"


def _drain_path_walk_workers(mod_db: PathWalkModuleDb) -> None:
    """Ingest tier-0 background work needed during verification (no full DB build)."""
    mod_db.drain_background_workers(wait_all=True)


def build_path_walk_db_full(mod_db: PathWalkModuleDb) -> int:
    """
    Post-verify full tier-1 DB build (opt-in via ``HIERWALK_PW_DB_BUILD=after_verify``).

    Returns the number of files queued for prefetch.
    """
    from hierwalk.perf import pw_db_build_mode, pw_db_prefetch_wait_on_exit

    if pw_db_build_mode() != "after_verify":
        return 0
    queued = mod_db.start_background_tier1_prefetch()
    mod_db.drain_background_workers(wait_all=pw_db_prefetch_wait_on_exit())
    return queued


def finalize_path_walk_suite_db() -> int:
    """Run post-verify full DB build on the shared flat-suite session, if configured."""
    if _suite_session is None:
        return 0
    return build_path_walk_db_full(_suite_session.mod_db)


def create_path_walk_index(
    fl: FilelistResult,
    top: str,
    *,
    defines: Mapping[str, str],
    ignore_paths: Sequence[str] = (),
    ignore_path_files: Sequence[str] = (),
    ignore_modules: Sequence[str] = (),
    ignore_filelists: Sequence[str] = (),
    cache_dir: Optional[Path] = None,
    no_cache: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
    trace_stream: Optional[TextIO] = None,
    trace_log_fh: Optional[TextIO] = None,
    path_digests: Mapping[str, str] | None = None,
    jobs: int = 0,
) -> Tuple[DesignIndex, PathWalkModuleDb]:
    from hierwalk.manifest import LazyPathDigests, set_digest_scope
    path_patterns, module_patterns, filelist_patterns = resolve_ignore_path_patterns(
        ignore_paths,
        ignore_path_files=ignore_path_files,
        ignore_modules=ignore_modules,
        ignore_filelists=ignore_filelists,
    )
    stubs = scan_library_modules(
        [str(p) for p in fl.library_files],
        [str(p) for p in fl.library_dirs],
        libexts=fl.libexts,
        skip_path_patterns=path_patterns,
        jobs=1,
    )
    merged = dict(stubs)
    index = DesignIndex._assemble(
        merged,
        path_patterns=list(path_patterns),
        module_patterns=list(module_patterns),
        filelist_patterns=list(filelist_patterns),
        library_files=[str(p) for p in fl.library_files],
        library_dirs=[str(p) for p in fl.library_dirs],
        libexts=list(fl.libexts),
        file_via_filelist={
            str(Path(k).resolve()): v
            for k, v in (fl.source_via_filelist or {}).items()
        },
        file_filelist_chain={
            str(Path(k).resolve()): v
            for k, v in (fl.source_filelist_chain or {}).items()
        },
        preprocess_include_dirs=[str(p) for p in fl.include_dirs],
        preprocess_defines=dict(defines),
    )
    all_sources = [str(Path(p).resolve()) for p in fl.source_files]
    sources, _ignored_sources = partition_sources(
        all_sources,
        path_patterns,
        filelist_patterns=filelist_patterns,
        file_via_filelist={
            str(Path(k).resolve()): v
            for k, v in (fl.source_via_filelist or {}).items()
        },
        file_filelist_chain={
            str(Path(k).resolve()): v
            for k, v in (fl.source_filelist_chain or {}).items()
        },
    )
    if path_digests is None:
        path_digests = LazyPathDigests.for_paths(all_sources, jobs=jobs)
    set_digest_scope(path_digests)
    cache_key = path_walk_db_cache_key(
        sources,
        defines=defines,
        include_dirs=[str(p) for p in fl.include_dirs],
        skip_path_patterns=path_patterns,
        path_digests=path_digests,
    )
    def _db_trace(msg: str) -> None:
        _path_walk_trace_emit(
            msg,
            trace_stream=trace_stream,
            trace_log_fh=trace_log_fh,
            on_progress=on_progress,
        )

    from hierwalk.filelist import filelist_provenance_maps

    via_map, _chain_map = filelist_provenance_maps(fl)
    mod_db = PathWalkModuleDb(
        sources,
        index,
        include_dirs=[str(p) for p in fl.include_dirs],
        defines=dict(defines),
        skip_path_patterns=path_patterns,
        ignore_module_patterns=module_patterns,
        cache_dir=cache_dir,
        cache_key=cache_key,
        no_cache=no_cache,
        on_trace=_db_trace,
        on_progress=on_progress,
        file_via_filelist=via_map,
        filelist_children={
            str(k): list(v) for k, v in (fl.filelist_children or {}).items()
        },
        path_digests=path_digests,
        jobs=jobs,
    )
    from hierwalk.progress import ProgressHeartbeat

    with ProgressHeartbeat(
        on_progress or (lambda _msg: None),
        "path-walk",
        enabled=on_progress is not None,
        get_detail=mod_db.heartbeat_detail,
    ):
        mod_db.remember_index_modules()
        if on_progress:
            on_progress(mod_db.format_status_line())
        mod_db._set_phase("mapping", detail=f"top {top}")
        top_file = mod_db.find_module_decl_file(top)
        if not top_file:
            raise ValueError(f"top module {top!r} not found in filelist sources")
        if on_progress:
            on_progress(f"path-walk: seed top {top} ({Path(top_file).name})")
        mod_db.seed_top_module(top, top_file)
    return index, mod_db


def run_path_walk_connect(
    request: ConnectivityRequest,
    fl: FilelistResult,
    *,
    top: str = "",
    extra_defines: Mapping[str, str] | None = None,
    ignore_paths: Sequence[str] = (),
    ignore_path_files: Sequence[str] = (),
    ignore_modules: Sequence[str] = (),
    ignore_filelists: Sequence[str] = (),
    cache_dir: Optional[Path] = None,
    no_cache: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
    trace_stream: Optional[TextIO] = None,
    trace_log_path: Optional[Path] = None,
    reuse_suite_session: bool = False,
    jobs: int = 0,
    connect_jobs: int = 0,
    connect_output_dir: Optional[Path] = None,
    connect_output_name: str = "conn.tsv",
    connect_phase: str = "both",
) -> Tuple[ConnectivityBatchResult, DesignIndex, PathWalkState]:
    """
    Path-walk batch connectivity: on-demand RTL + shared :class:`ConnectivitySession`.

    Hierarchy walk honors ``jobs`` at trie branch points. ``connect_jobs`` runs
    text-COI in parallel (shared ``mod_cache``); pipeline mode interleaves
    per-check hierarchy with connect workers when ``connect_jobs`` > 1.
    """
    from hierwalk.connectivity import _resolve_connect_jobs
    from hierwalk.perf import connect_jobs_from_env
    defines = dict(fl.defines)
    defines.update(extra_defines or {})
    defines.update(request.defines)
    phase = connect_phase if connect_phase in ("text", "logical", "both") else "both"
    do_text = phase in ("text", "both")
    do_logical = phase in ("logical", "both")
    effective_connect_jobs = connect_jobs if connect_jobs != 0 else connect_jobs_from_env()
    conn_workers = _resolve_connect_jobs(effective_connect_jobs, len(request.checks))
    use_connect_pipeline = (
        do_text
        and conn_workers > 1
        and len(request.checks) >= 2
    )

    trace_log_fh: Optional[TextIO] = None
    opened_log = False
    if trace_log_path is not None:
        trace_log_fh, opened_log = _acquire_path_walk_trace_log(
            trace_log_path,
            phase=phase,
            reuse_suite_session=reuse_suite_session,
        )
    try:
        if reuse_suite_session:
            session_extra = dict(extra_defines or {})
            session_extra.update(request.defines)
            suite = acquire_path_walk_session(
                fl,
                top=top,
                extra_defines=session_extra,
                ignore_paths=ignore_paths,
                ignore_path_files=ignore_path_files,
                ignore_modules=ignore_modules,
                ignore_filelists=ignore_filelists,
                cache_dir=cache_dir,
                no_cache=no_cache,
                on_progress=on_progress,
                trace_stream=trace_stream,
                trace_log_fh=trace_log_fh,
                jobs=jobs,
            )
            index = suite.index
            top_name = suite.top_name
            state = suite.state
            if do_text:
                _extend_path_walk_connect(
                    state,
                    request,
                    on_progress=on_progress,
                    jobs=jobs,
                )
            elif do_logical:
                _reset_specs_for_logical_rewalk(
                    state,
                    endpoint_specs_from_request(request),
                )
                _extend_path_walk_connect(
                    state,
                    request,
                    on_progress=on_progress,
                    jobs=jobs,
                )
        else:
            index, mod_db = create_path_walk_index(
                fl,
                top,
                defines=defines,
                ignore_paths=ignore_paths,
                ignore_path_files=ignore_path_files,
                ignore_modules=ignore_modules,
                ignore_filelists=ignore_filelists,
                cache_dir=cache_dir,
                no_cache=no_cache,
                on_progress=on_progress,
                trace_stream=trace_stream,
                trace_log_fh=trace_log_fh,
                jobs=jobs,
            )
            tops = resolve_top_modules(index, top=top, filelist_tops=fl.top_modules)
            top_name = tops[0]

            if use_connect_pipeline and not reuse_suite_session:
                state = _init_path_walk_state_shell(
                    index,
                    top_name,
                    mod_db,
                    on_progress=on_progress,
                    trace_stream=trace_stream,
                    trace_log_fh=trace_log_fh,
                )
            else:
                state = build_path_walk_state(
                    index,
                    top_name,
                    request,
                    mod_db,
                    on_progress=on_progress,
                    trace_stream=trace_stream,
                    trace_log_fh=trace_log_fh,
                    close_trace_log=False,
                    jobs=jobs,
                )
        state._sync_db_stats()
        if on_progress:
            on_progress(
                f"path-walk: {len(state.rows_by_path)} instance row(s), "
                f"{state.stats.modules_loaded} module(s) loaded, "
                f"tier0={state.stats.files_regex_scanned} tier1={state.stats.files_validated} "
                f"cache={state.stats.cache_regex_hits}+{state.stats.cache_validated_hits}"
            )

        if do_text and not do_logical:
            _drain_path_walk_workers(state.mod_db)
        elif do_logical and not do_text and reuse_suite_session:
            pass
        elif do_text:
            _drain_path_walk_workers(state.mod_db)
        elif do_logical:
            if state.mod_db.defer_count() and not state.stats.recovery_stalled:
                state.run_recovery_pass()
            _drain_path_walk_workers(state.mod_db)

        from hierwalk.models import ElabIndex

        walk_rows = state.rows()
        conn_session = ConnectivitySession(
            rows=walk_rows,
            index=index,
            top=top_name,
            defines=defines,
            strict_generate=request.strict_generate,
            ff_barrier=not request.include_ff,
            over_approximate_if=request.over_approximate_if,
            elab_index=ElabIndex.from_rows_by_path(
                state.rows_by_path,
                rows=walk_rows,
            ),
        )
        from hierwalk.connect_artifacts import (
            apply_connect_logical_phase,
            connect_output_paths,
            format_connect_hierarchy_tsv,
            merge_refined_connect_results,
            normalize_connect_results,
            prepare_text_connect_request,
            reorder_connect_results_to_checks,
            require_connect_phase_tsv,
            resolve_connect_output_dir,
            snapshot_connect_text_phase,
        )
        from hierwalk.verification_timing import get_active_recorder

        bind_path_walk_phase_emit(state._emit_walk)
        timing_rec = get_active_recorder()
        resolved_output_dir = resolve_connect_output_dir(
            connect_output_dir,
            top=top_name,
            cache_dir=cache_dir,
        )
        out_paths = connect_output_paths(resolved_output_dir, connect_output_name)
        try:
            batch: Optional[ConnectivityBatchResult] = None
            if do_text:
                if timing_rec is not None:
                    timing_rec.begin_step("text-conn", "connect-coi")
                t_coi = time.perf_counter()
                text_request = prepare_text_connect_request(request)
                text_results: list = []
                text_modules_cached: Optional[int] = None
                coi_error = ""
                conn_session.resolve_param_dims = False
                try:
                    state._emit_walk(
                        f"connect-coi begin checks={len(text_request.checks)} "
                        f"rows={len(walk_rows)} resolve_param_dims=0 "
                        f"connect_jobs={conn_workers}"
                    )
                    try:
                        if use_connect_pipeline and not reuse_suite_session:
                            batch = _pipeline_path_walk_text_conn(
                                state,
                                request,
                                conn_session,
                                hierarchy_jobs=jobs,
                                connect_jobs=effective_connect_jobs,
                                on_progress=on_progress,
                            )
                        else:
                            batch = conn_session.run_text_request(
                                text_request,
                                on_progress=on_progress,
                                jobs=effective_connect_jobs,
                                record_timing=True,
                            )
                        text_results = list(batch.results)
                        text_modules_cached = batch.modules_cached
                    except Exception as exc:
                        coi_error = f"connect-coi failed: {exc!r}"
                        state._emit_walk(coi_error)
                        text_results = []
                    text_results = normalize_connect_results(
                        text_request,
                        text_results,
                        conn_session,
                        coi_error=coi_error,
                    )
                    text_results = reorder_connect_results_to_checks(
                        request.checks,
                        text_results,
                    )
                    if text_results:
                        batch = ConnectivityBatchResult(
                            results=tuple(text_results),
                            modules_cached=(
                                text_modules_cached
                                if text_modules_cached is not None
                                else conn_session.modules_cached
                            ),
                        )
                    snapshot_connect_text_phase(text_results)
                    coi_ms = (time.perf_counter() - t_coi) * 1000.0
                    state._emit_walk(
                        f"connect-coi done checks={len(text_results)} "
                        f"modules_cached={conn_session.modules_cached} "
                        f"ms={coi_ms:.1f}"
                    )
                    state._emit_walk(
                        f"connect-text-conn done checks={len(text_results)} "
                        f"ms={coi_ms:.1f}"
                    )
                finally:
                    written = require_connect_phase_tsv(
                        out_paths.text_tsv,
                        text_results,
                        phase="text",
                        modules_cached=text_modules_cached,
                        rows_by_path=state.rows_by_path,
                    )
                    out_paths.hierarchy_text_tsv.write_text(
                        format_connect_hierarchy_tsv(
                            text_results,
                            state.rows_by_path,
                            phase="text",
                            signal_tails=state._signal_tail_records,
                            index=index,
                            top=top_name,
                        ),
                        encoding="utf-8",
                    )
                    state._emit_walk(
                        f"connect-text-conn written {written.resolve()}"
                    )
                    if timing_rec is not None:
                        timing_rec.end_step()
                state.stats.checks_run = len(text_results)

            if do_logical:
                if timing_rec is not None:
                    timing_rec.begin_step("logical-conn", "activation-audit")
                t_act = time.perf_counter()
                t_refine = time.perf_counter()
                logical_results: list = []
                logical_modules_cached: Optional[int] = None
                logical_coi_error = ""
                conn_session.resolve_param_dims = True
                conn_session.clear_cache()
                try:
                    finalize_logical_walk_before_connect(state, request)
                    walk_rows = state.rows()
                    conn_session.rows = walk_rows
                    conn_session.elab_index = ElabIndex.from_rows_by_path(
                        state.rows_by_path,
                        rows=walk_rows,
                    )
                    conn_session.clear_cache()
                    t_recoi = time.perf_counter()
                    try:
                        refined_batch = conn_session.run_request(
                            request,
                            jobs=effective_connect_jobs,
                            on_progress=on_progress,
                        )
                    except Exception as exc:
                        logical_coi_error = f"connect-coi failed: {exc!r}"
                        state._emit_walk(logical_coi_error)
                        refined_batch = ConnectivityBatchResult(
                            results=(),
                            modules_cached=conn_session.modules_cached,
                        )
                    if batch is not None:
                        merge_refined_connect_results(
                            batch.results,
                            refined_batch.results,
                        )
                    else:
                        batch = refined_batch
                    state._emit_walk(
                        f"connect-logical-walk done rows={len(state.rows_by_path)} "
                        f"recoi_ms={(time.perf_counter() - t_recoi) * 1000.0:.1f} "
                        f"ms={(time.perf_counter() - t_refine) * 1000.0:.1f}"
                    )
                    drain_audit = getattr(
                        state.mod_db, "drain_activation_audit", None
                    )
                    if callable(drain_audit):
                        drain_audit(wait=True)
                    emit_audit = getattr(
                        state.mod_db, "emit_activation_audit_report", None
                    )
                    if callable(emit_audit):
                        emit_audit()
                    sync_activation_to_walk_rows(state.mod_db, state)
                    apply_connect_logical_phase(
                        batch.results,
                        state.rows_by_path,
                        run_activation=True,
                    )
                    logical_results = normalize_connect_results(
                        request,
                        batch.results,
                        conn_session,
                        coi_error=logical_coi_error,
                    )
                    logical_results = reorder_connect_results_to_checks(
                        request.checks,
                        logical_results,
                    )
                    batch = ConnectivityBatchResult(
                        results=tuple(logical_results),
                        modules_cached=batch.modules_cached,
                    )
                    logical_modules_cached = batch.modules_cached
                    logical_ms = (time.perf_counter() - t_act) * 1000.0
                    state._emit_walk(
                        f"connect-logical-conn done checks={len(batch.results)} "
                        f"ms={logical_ms:.1f}"
                    )
                finally:
                    written = require_connect_phase_tsv(
                        out_paths.logical_tsv,
                        logical_results,
                        phase="logical",
                        modules_cached=logical_modules_cached,
                        rows_by_path=state.rows_by_path,
                    )
                    out_paths.hierarchy_logical_tsv.write_text(
                        format_connect_hierarchy_tsv(
                            logical_results,
                            state.rows_by_path,
                            phase="logical",
                            signal_tails=state._signal_tail_records,
                            index=index,
                            top=top_name,
                        ),
                        encoding="utf-8",
                    )
                    state._emit_walk(
                        f"connect-logical-conn written {written.resolve()}"
                    )
                    if timing_rec is not None:
                        timing_rec.end_step()
                state.stats.checks_run = len(logical_results)
            assert batch is not None
        finally:
            bind_path_walk_phase_emit(None)
        return batch, index, state
    finally:
        if trace_log_fh is not None:
            if reuse_suite_session:
                if _suite_session is not None:
                    _suite_session.trace_log_fh = trace_log_fh
                    _suite_session.trace_log_path = trace_log_path
            elif opened_log:
                trace_log_fh.close()