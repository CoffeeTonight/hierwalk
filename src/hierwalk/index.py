"""Pre-built module/file index for fast hierarchy elaboration."""

from __future__ import annotations

import fnmatch
import os
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import Callable, Dict, List, Literal, Mapping, Optional, Sequence, Tuple

from hierwalk.generate_fold import (
    body_without_generate_regions,
    needs_generate_fold,
    prepare_body_for_instance_scan,
)
from hierwalk.inst_scan import (
    iter_module_blocks,
    scan_hierarchy_instances,
    slim_body_for_instance_scan,
)
from hierwalk.ignore_path import (
    partition_sources,
    resolve_ignore_path_patterns,
    scan_ignore_path_stubs,
    source_path_matches,
)
from hierwalk.library_scan import scan_library_modules
from hierwalk.models import FilelistLinkInfo, InstanceEdge, ModuleRecord
from hierwalk.params import (
    body_param_scan_skipped,
    collect_index_module_params,
    collect_module_params,
    dim_exprs_from_inst_names,
    instance_param_exprs,
    parse_param_pairs,
    resolve_param_map,
    split_module_header,
    strip_body_param_declarations,
)
from hierwalk.perf import (
    body_param_scan_max,
    log_large_module_skips,
    slow_file_log_threshold_sec,
)


def _scan_module_body(
    body: str,
    raw_params: Mapping[str, str],
    *,
    parent_ctx: Optional[Mapping[str, str]] = None,
    overrides: Optional[Mapping[str, str]] = None,
    compile_defines: Optional[Mapping[str, str]] = None,
) -> List[InstanceEdge]:
    pmap = resolve_param_map(raw_params, overrides=overrides, parent=parent_ctx)
    fold_ctx = dict(compile_defines or {})
    fold_ctx.update(pmap)
    folded = prepare_body_for_instance_scan(body, fold_ctx)
    return scan_hierarchy_instances(folded, param_map=fold_ctx)


def _ctx_key(pmap: Mapping[str, str]) -> str:
    return "|".join(f"{k}={v}" for k, v in sorted(pmap.items()))


def _module_name_ignored(name: str, patterns: List[str]) -> bool:
    for pat in patterns:
        if not pat:
            continue
        if any(ch in pat for ch in ("*", "?", "[")):
            if fnmatch.fnmatchcase(name, pat):
                return True
        elif pat == name:
            return True
    return False


ScanMode = Literal["parse", "ignore"]

_SCAN_PREPROCESSED: Dict[str, str] = {}


def _install_scan_preprocessed(snapshot: Mapping[str, str]) -> None:
    """Seed worker-local preprocessed map (``spawn``) or share via ``fork`` COW."""
    global _SCAN_PREPROCESSED
    _SCAN_PREPROCESSED = dict(snapshot)


def _scan_file_from_snapshot(item: Tuple[str, ScanMode]) -> Dict[str, ModuleRecord]:
    fpath, mode = item
    text = _preprocessed_text(_SCAN_PREPROCESSED, fpath)
    return _scan_file_task((fpath, text, mode))


def _resolve_jobs(jobs: int, num_tasks: int) -> int:
    if jobs < 0:
        return 1
    if jobs == 0:
        cpu = os.cpu_count() or 1
        return max(1, min(cpu, num_tasks))
    return max(1, min(jobs, num_tasks))


def _scan_file_task(item: Tuple[str, str, ScanMode]) -> Dict[str, ModuleRecord]:
    """Picklable per-file scan (file-module table row)."""
    fpath, text, mode = item
    if mode == "ignore":
        return scan_ignore_path_stubs(text, fpath)
    return scan_preprocessed(text, fpath)


def _log_slow_index_file(
    fpath: str,
    *,
    preprocess_sec: float,
    scan_sec: float,
    text_bytes: int,
) -> None:
    import sys

    total = preprocess_sec + scan_sec
    threshold = slow_file_log_threshold_sec()
    if threshold is None or total < threshold:
        return
    mb = text_bytes / (1024 * 1024)
    print(
        f"index: slow file {total:.1f}s "
        f"(preprocess {preprocess_sec:.1f}s, scan {scan_sec:.1f}s, "
        f"{mb:.1f} MiB preprocessed) {fpath}",
        file=sys.stderr,
        flush=True,
    )


def _preprocess_scan_file_task(
    item: Tuple[str, Tuple[str, ...], Tuple[Tuple[str, str], ...], ScanMode, Tuple[str, ...]],
) -> Dict[str, ModuleRecord]:
    """Picklable fused preprocess + scan (avoids retaining full preprocessed text)."""
    fpath, inc_dirs, define_items, mode, skip_patterns = item
    path = Path(fpath)
    from hierwalk.lazy_scope import lazy_processing_enabled
    from hierwalk.preprocess import preprocess_file, preprocess_file_for_index

    inc = [Path(p) for p in inc_dirs]
    defs: Dict[str, str] = dict(define_items)
    preprocess_fn = (
        preprocess_file_for_index if lazy_processing_enabled() else preprocess_file
    )
    t0 = time.perf_counter()
    text = preprocess_fn(
        path,
        inc,
        defs,
        set(),
        skip_path_patterns=skip_patterns,
    )
    t_prep = time.perf_counter() - t0
    mods = scan_preprocessed(text, fpath)
    t_scan = time.perf_counter() - t0 - t_prep
    _log_slow_index_file(
        fpath,
        preprocess_sec=t_prep,
        scan_sec=t_scan,
        text_bytes=len(text),
    )
    return mods


def _merge_file_scans(
    merged: Dict[str, ModuleRecord],
    per_file: Dict[str, ModuleRecord],
) -> None:
    for name, rec in per_file.items():
        if name not in merged:
            merged[name] = rec


def _preprocessed_text(preprocessed: Mapping[str, str], fpath: str) -> str:
    for key in (fpath, str(Path(fpath)), str(Path(fpath).resolve())):
        hit = preprocessed.get(key)
        if hit is not None:
            return hit
    raise KeyError(fpath)


def _inject_referenced_ignore_stubs(
    merged: Dict[str, ModuleRecord],
    *,
    path_patterns: Sequence[str],
    module_patterns: Sequence[str],
    filelist_patterns: Sequence[str] = (),
) -> None:
    """
    Add ignorePath stubs for instance targets not defined in parsed RTL.

    Avoids reading every file under ignore-path directories; hierarchy references
    alone determine which module names need a stop boundary.
    """
    if not path_patterns and not module_patterns and not filelist_patterns:
        return
    referenced: set[str] = set()
    for rec in merged.values():
        if rec.stop_reason:
            continue
        for edge in rec.instances:
            child = edge.child_module
            if child and child not in merged:
                referenced.add(child)
    for mod_name in sorted(referenced):
        if mod_name in merged:
            continue
        if _module_name_ignored(mod_name, list(module_patterns)):
            merged[mod_name] = ModuleRecord(
                module_name=mod_name,
                file_path="",
                stop_reason="ignorePath",
                is_blackbox=True,
            )
        elif path_patterns or filelist_patterns:
            merged[mod_name] = ModuleRecord(
                module_name=mod_name,
                file_path="",
                stop_reason="ignorePath",
                is_blackbox=True,
            )


def _build_merged_from_sources(
    parse_sources: List[str],
    ignore_sources: List[str],
    *,
    include_dirs: Sequence[str],
    defines: Mapping[str, str],
    jobs: int = 0,
    low_memory: bool = False,
    skip_path_patterns: Sequence[str] = (),
    on_progress: Optional[Callable[[str], None]] = None,
    file_via_filelist: Optional[Mapping[str, str]] = None,
) -> Dict[str, ModuleRecord]:
    """Default: parallel preprocess then in-memory scan (preprocessed map discarded)."""
    merged: Dict[str, ModuleRecord] = {}
    if low_memory:
        if parse_sources:
            _merge_file_scans(
                merged,
                _scan_sources_fused(
                    parse_sources,
                    [],
                    include_dirs=include_dirs,
                    defines=defines,
                    jobs=jobs,
                    skip_path_patterns=skip_path_patterns,
                    on_progress=on_progress,
                    file_via_filelist=file_via_filelist,
                ),
            )
        return merged
    if not parse_sources:
        return merged
    from hierwalk.preprocess import preprocess_sources

    preprocessed = preprocess_sources(
        parse_sources,
        include_dirs,
        defines,
        jobs=jobs,
        skip_path_patterns=skip_path_patterns,
        on_progress=on_progress,
        file_via_filelist=file_via_filelist,
    )
    _merge_file_scans(
        merged,
        _scan_sources(
            preprocessed,
            parse_sources,
            [],
            jobs=jobs,
            on_progress=on_progress,
            file_via_filelist=file_via_filelist,
        ),
    )
    return merged


def _scan_sources(
    preprocessed: Mapping[str, str],
    parse_sources: List[str],
    ignore_sources: List[str],
    *,
    jobs: int = 0,
    on_progress: Optional[Callable[[str], None]] = None,
    file_via_filelist: Optional[Mapping[str, str]] = None,
) -> Dict[str, ModuleRecord]:
    path_tasks: List[Tuple[str, ScanMode]] = [(fpath, "parse") for fpath in parse_sources]
    merged: Dict[str, ModuleRecord] = {}
    if not path_tasks:
        return merged

    total = len(path_tasks)
    workers = _resolve_jobs(jobs, total)
    t0 = time.perf_counter()
    if on_progress:
        jobs_note = "auto" if jobs == 0 else str(jobs)
        on_progress(
            f"index: scanning 0/{total} files "
            f"({workers} workers, jobs={jobs_note})"
        )
    from hierwalk.progress import format_work_location, maybe_track_work

    def _report_progress(i: int, fpath: str) -> None:
        maybe_track_work(
            on_progress,
            fpath,
            index=i,
            total=total,
            via_map=file_via_filelist,
        )
        if on_progress and (i == total or i % 500 == 0):
            loc = format_work_location(
                fpath,
                index=i,
                total=total,
                via_map=file_via_filelist,
            )
            on_progress(f"index: scanning {i}/{total} files — {loc}")

    if workers == 1 or total <= 1:
        for i, fpath in enumerate(parse_sources, start=1):
            text = _preprocessed_text(preprocessed, fpath)
            _merge_file_scans(merged, _scan_file_task((fpath, text, "parse")))
            _report_progress(i, fpath)
    else:
        scan_snapshot = dict(preprocessed)
        scanned = False
        try:
            from hierwalk.manifest import scan_chunksize

            chunk = scan_chunksize(total, workers)
            with ProcessPoolExecutor(
                max_workers=workers,
                initializer=_install_scan_preprocessed,
                initargs=(scan_snapshot,),
            ) as pool:
                for i, per_file in enumerate(
                    pool.map(_scan_file_from_snapshot, path_tasks, chunksize=chunk),
                    start=1,
                ):
                    _merge_file_scans(merged, per_file)
                    _report_progress(i, path_tasks[i - 1][0])
            scanned = True
        except (OSError, PermissionError, RuntimeError) as exc:
            if on_progress:
                on_progress(
                    f"index: parallel scan workers failed ({exc!r}); "
                    "retrying with thread pool"
                )
        if not scanned:
            try:
                text_tasks = [
                    (fpath, _preprocessed_text(preprocessed, fpath), "parse")
                    for fpath in parse_sources
                ]
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for i, per_file in enumerate(
                        pool.map(_scan_file_task, text_tasks),
                        start=1,
                    ):
                        _merge_file_scans(merged, per_file)
                        _report_progress(i, text_tasks[i - 1][0])
                scanned = True
            except (OSError, PermissionError, RuntimeError) as exc2:
                if on_progress:
                    on_progress(
                        f"index: thread scan failed ({exc2!r}); "
                        "falling back to serial"
                    )
        if not scanned:
            for i, fpath in enumerate(parse_sources, start=1):
                text = _preprocessed_text(preprocessed, fpath)
                _merge_file_scans(merged, _scan_file_task((fpath, text, "parse")))
                _report_progress(i, fpath)

    elapsed = time.perf_counter() - t0
    if on_progress and total:
        rate = total / elapsed if elapsed > 0 else 0.0
        jobs_note = "auto" if jobs == 0 else str(jobs)
        on_progress(
            f"index: scan done {total} files in {elapsed:.1f}s "
            f"({rate:.1f} files/s, {workers} workers, jobs={jobs_note})"
        )
    return merged


def _scan_sources_fused(
    parse_sources: List[str],
    ignore_sources: List[str],
    *,
    include_dirs: Sequence[str],
    defines: Mapping[str, str],
    jobs: int = 0,
    skip_path_patterns: Sequence[str] = (),
    on_progress: Optional[Callable[[str], None]] = None,
    file_via_filelist: Optional[Mapping[str, str]] = None,
) -> Dict[str, ModuleRecord]:
    define_items = tuple(sorted(defines.items()))
    inc_dirs = tuple(str(Path(p)) for p in include_dirs)
    skip_tuple = tuple(skip_path_patterns)
    tasks: List[
        Tuple[str, Tuple[str, ...], Tuple[Tuple[str, str], ...], ScanMode, Tuple[str, ...]]
    ] = [
        (fpath, inc_dirs, define_items, "parse", skip_tuple) for fpath in parse_sources
    ]
    merged: Dict[str, ModuleRecord] = {}
    if not tasks:
        return merged

    workers = _resolve_jobs(jobs, len(tasks))
    total = len(tasks)
    t0 = time.perf_counter()
    if on_progress:
        jobs_note = "auto" if jobs == 0 else str(jobs)
        on_progress(
            f"index: scanning 0/{total} files "
            f"({workers} workers, jobs={jobs_note}, fused)"
        )
    from hierwalk.progress import format_work_location, maybe_track_work
    from hierwalk.preprocess import (
        _install_preprocess_caches,
        _snapshot_include_cache,
        _snapshot_source_preprocess_cache,
        _warm_include_cache_for_sources,
    )

    inc_paths = [Path(p) for p in include_dirs]
    _warm_include_cache_for_sources(
        parse_sources,
        inc_paths,
        defines,
        skip_path_patterns=skip_tuple,
        jobs=jobs,
        on_progress=on_progress,
        file_via_filelist=file_via_filelist,
    )
    cache_snapshot = _snapshot_include_cache()
    source_cache_snapshot = _snapshot_source_preprocess_cache()

    if workers == 1:
        for i, task in enumerate(tasks, start=1):
            _merge_file_scans(merged, _preprocess_scan_file_task(task))
            maybe_track_work(
                on_progress,
                task[0],
                index=i,
                total=total,
                via_map=file_via_filelist,
            )
            if on_progress and (i == total or i % 500 == 0):

                loc = format_work_location(
                    task[0],
                    index=i,
                    total=total,
                    via_map=file_via_filelist,
                )
                on_progress(f"index: scanning {i}/{total} sources — {loc}")
    else:
        try:
            from hierwalk.manifest import scan_chunksize

            chunk = scan_chunksize(total, workers)
            with ProcessPoolExecutor(
                max_workers=workers,
                initializer=_install_preprocess_caches,
                initargs=(cache_snapshot, source_cache_snapshot),
            ) as pool:
                for i, per_file in enumerate(
                    pool.map(_preprocess_scan_file_task, tasks, chunksize=chunk),
                    start=1,
                ):
                    _merge_file_scans(merged, per_file)
                    fpath = tasks[i - 1][0]
                    maybe_track_work(
                        on_progress,
                        fpath,
                        index=i,
                        total=total,
                        via_map=file_via_filelist,
                    )
                    if on_progress and (i == total or i % 500 == 0):
                        loc = format_work_location(
                            fpath,
                            index=i,
                            total=total,
                            via_map=file_via_filelist,
                        )
                        on_progress(f"index: scanning {i}/{total} sources — {loc}")
        except (OSError, PermissionError, RuntimeError):
            for task in tasks:
                _merge_file_scans(merged, _preprocess_scan_file_task(task))

    elapsed = time.perf_counter() - t0
    if on_progress and total:
        rate = total / elapsed if elapsed > 0 else 0.0
        jobs_note = "auto" if jobs == 0 else str(jobs)
        on_progress(
            f"index: fused scan done {total} files in {elapsed:.1f}s "
            f"({rate:.1f} files/s, {workers} workers, jobs={jobs_note})"
        )
    return merged


def _module_header_body(text: str, mod_name: str) -> Tuple[str, str]:
    for block in iter_module_blocks(text):
        if block["name"] == mod_name:
            return split_module_header(block["chunk"])
    return "", ""


def _instances_for_index(
    header: str,
    body: str,
    *,
    max_body_bytes: Optional[int] = None,
) -> Tuple[Dict[str, str], List[InstanceEdge], bool]:
    """
    Index-time scan: find instances first, then collect only params they need.

    Generate bodies are deferred to :meth:`DesignIndex.instances_for`, but
    instances declared outside ``generate`` are still indexed at tier-1.
    """
    defer_fold = needs_generate_fold(body)
    header_params = parse_param_pairs(header)
    if defer_fold:
        scan_src = body_without_generate_regions(body)
    else:
        scan_src = body
    scan_body = slim_body_for_instance_scan(strip_body_param_declarations(scan_src))
    edges = scan_hierarchy_instances(
        scan_body,
        param_map=resolve_param_map(header_params),
    )
    inst_exprs = [
        *dim_exprs_from_inst_names(e.inst_name for e in edges),
        *instance_param_exprs(edges),
    ]
    raw_params = collect_index_module_params(header, body, inst_exprs)
    if set(raw_params) != set(header_params):
        edges = scan_hierarchy_instances(
            scan_body,
            param_map=resolve_param_map(raw_params),
        )
    return raw_params, edges, defer_fold


def scan_preprocessed(text: str, file_path: str) -> Dict[str, ModuleRecord]:
    out: Dict[str, ModuleRecord] = {}
    param_limit = body_param_scan_max()
    for block in iter_module_blocks(text):
        name = block["name"]
        header, body = split_module_header(block["chunk"])
        if body_param_scan_skipped(body, max_body_bytes=param_limit) and log_large_module_skips():
            import sys

            print(
                f"index: instance-first params ({len(body)} B) {file_path} :: {name}",
                file=sys.stderr,
            )
        raw_params, edges, defer_fold = _instances_for_index(
            header,
            body,
            max_body_bytes=param_limit,
        )
        is_interface = block["kind"] == "interface"
        out[name] = ModuleRecord(
            module_name=name,
            file_path=file_path,
            body="",
            raw_params=dict(raw_params),
            instances=edges,
            needs_generate_fold=defer_fold,
            is_interface=is_interface,
        )
    return out


class DesignIndex:
    """
    File/module maps built once from RTL.

    Elaboration looks up ``modules[child_type]`` and reuses default instance
    lists; non-default parameter contexts are cached by ``(module, ctx_key)``.
    """

    def __init__(
        self,
        modules: Mapping[str, ModuleRecord],
        *,
        ignore_path_patterns: Optional[List[str]] = None,
        ignore_module_patterns: Optional[List[str]] = None,
        ignore_filelist_patterns: Optional[List[str]] = None,
        file_via_filelist: Optional[Mapping[str, str]] = None,
        file_filelist_chain: Optional[Mapping[str, str]] = None,
        filelist_info: Optional[Mapping[str, FilelistLinkInfo]] = None,
        filelist_children: Optional[Mapping[str, List[str]]] = None,
        filelist_edges: Optional[List[tuple[str, str, str]]] = None,
        preprocess_include_dirs: Optional[Sequence[str]] = None,
        preprocess_defines: Optional[Mapping[str, str]] = None,
        preprocessed_sources: Optional[Mapping[str, str]] = None,
        low_memory: bool = False,
    ) -> None:
        self.modules: Dict[str, ModuleRecord] = dict(modules)
        self.ignore_path_patterns: List[str] = list(ignore_path_patterns or [])
        self.ignore_module_patterns: List[str] = list(ignore_module_patterns or [])
        self.ignore_filelist_patterns: List[str] = list(ignore_filelist_patterns or [])
        self.file_via_filelist: Dict[str, str] = dict(file_via_filelist or {})
        self.file_filelist_chain: Dict[str, str] = dict(file_filelist_chain or {})
        self.filelist_info: Dict[str, FilelistLinkInfo] = dict(filelist_info or {})
        self.filelist_children: Dict[str, List[str]] = {
            k: list(v) for k, v in (filelist_children or {}).items()
        }
        self.filelist_edges: List[tuple[str, str, str]] = list(filelist_edges or [])
        self._preprocess_include_dirs: List[str] = [
            str(Path(p)) for p in (preprocess_include_dirs or ())
        ]
        self._preprocess_defines: Dict[str, str] = dict(preprocess_defines or {})
        self._preprocessed_sources: Dict[str, str] = {
            str(Path(k)): v for k, v in (preprocessed_sources or {}).items()
        }
        self.low_memory: bool = low_memory
        self.index_jobs: int = 1
        self.file_modules: Dict[str, List[str]] = defaultdict(list)
        for name, rec in self.modules.items():
            self.file_modules[rec.file_path].append(name)
        for names in self.file_modules.values():
            names.sort()
        self._default_ctx: Dict[str, str] = {}
        self._instance_cache: Dict[Tuple[str, str], List[InstanceEdge]] = {}
        self._instance_cache_lock = threading.Lock()
        self._rebuild_default_ctx()

    def _rebuild_default_ctx(self) -> None:
        self._default_ctx = {}
        for name, rec in self.modules.items():
            if rec.needs_generate_fold:
                continue
            if rec.raw_params or rec.instances:
                self._default_ctx[name] = _ctx_key(resolve_param_map(rec.raw_params))

    def __getstate__(self) -> dict:
        """Pickle slim index: drop ephemeral caches; do not mutate the live index."""
        state = self.__dict__.copy()
        state.pop("_instance_cache_lock", None)
        state["_preprocessed_sources"] = {}
        state["_instance_cache"] = {}
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self._instance_cache_lock = threading.Lock()

    def _rebuild_file_modules(self) -> None:
        self.file_modules = defaultdict(list)
        for name, rec in self.modules.items():
            self.file_modules[rec.file_path].append(name)
        for names in self.file_modules.values():
            names.sort()

    def _source_text(self, file_path: str, *, full: bool = False) -> str:
        keys = (file_path, str(Path(file_path)), str(Path(file_path).resolve()))
        for key in keys:
            hit = self._preprocessed_sources.get(key)
            if hit is not None:
                return hit
        path = Path(file_path)
        if not path.is_file():
            return ""
        if self._preprocess_include_dirs or self._preprocess_defines:
            from hierwalk.lazy_scope import lazy_on_demand_full_preprocess, lazy_processing_enabled
            from hierwalk.preprocess import preprocess_file, preprocess_file_for_index

            inc = [Path(p) for p in self._preprocess_include_dirs]
            defs: Dict[str, str] = dict(self._preprocess_defines)
            if full or not (lazy_processing_enabled() and lazy_on_demand_full_preprocess()):
                return preprocess_file(path, inc, defs, set())
            return preprocess_file_for_index(path, inc, defs, set())
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""

    def module_body(self, mod_name: str) -> str:
        rec = self.modules.get(mod_name)
        if not rec or rec.stop_reason or rec.is_blackbox:
            return ""
        if rec.body:
            return rec.body
        text = self._source_text(rec.file_path)
        if not text:
            return ""
        best = ""
        for block in iter_module_blocks(text):
            if block["name"] != mod_name:
                continue
            _header, body = split_module_header(block["chunk"])
            if len(body.strip()) > len(best.strip()):
                best = body
        if best:
            rec.body = best
            return best
        return ""

    def strip_bodies_for_cache(self) -> None:
        """Explicit live-memory purge (not used on the pickle save path)."""
        for rec in self.modules.values():
            rec.body = ""
        self._preprocessed_sources.clear()
        self._instance_cache.clear()

    def invalidate_instance_cache_for_modules(self, mod_names: Sequence[str]) -> None:
        """Drop cached instance edges for *mod_names* after incremental index updates."""
        if not mod_names:
            return
        drop = set(mod_names)
        with self._instance_cache_lock:
            stale = [key for key in self._instance_cache if key[0] in drop]
            for key in stale:
                del self._instance_cache[key]

    def patch_files(
        self,
        changed_files: Sequence[str],
        removed_files: Sequence[str],
        *,
        include_dirs: Sequence[str] = (),
        defines: Mapping[str, str] | None = None,
        jobs: int = 0,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        if include_dirs:
            self._preprocess_include_dirs = [str(Path(p)) for p in include_dirs]
        if defines is not None:
            self._preprocess_defines = dict(defines)
        removed = set(removed_files)
        touched = set(changed_files) | removed
        for name in list(self.modules):
            if self.modules[name].file_path in touched:
                del self.modules[name]
        parse_sources, _ = partition_sources(
            list(changed_files),
            self.ignore_path_patterns,
            filelist_patterns=self.ignore_filelist_patterns,
            file_via_filelist=self.file_via_filelist,
            file_filelist_chain=self.file_filelist_chain,
        )
        if parse_sources:
            merged = _build_merged_from_sources(
                parse_sources,
                [],
                include_dirs=include_dirs,
                defines=defines or {},
                jobs=jobs,
                low_memory=self.low_memory,
                skip_path_patterns=self.ignore_path_patterns,
                on_progress=on_progress,
                file_via_filelist=self.file_via_filelist,
            )
            for name, rec in merged.items():
                self.modules[name] = rec
        self._rebuild_file_modules()
        self._instance_cache.clear()
        self._rebuild_default_ctx()

    @classmethod
    def _assemble(
        cls,
        merged: Dict[str, ModuleRecord],
        *,
        path_patterns: List[str],
        module_patterns: List[str],
        filelist_patterns: Optional[List[str]] = None,
        library_files: Optional[List[str]] = None,
        library_dirs: Optional[List[str]] = None,
        libexts: Optional[List[str]] = None,
        file_via_filelist: Optional[Mapping[str, str]] = None,
        file_filelist_chain: Optional[Mapping[str, str]] = None,
        filelist_info: Optional[Mapping[str, FilelistLinkInfo]] = None,
        filelist_children: Optional[Mapping[str, List[str]]] = None,
        filelist_edges: Optional[List[tuple[str, str, str]]] = None,
        index_jobs: int = 1,
        preprocess_include_dirs: Optional[Sequence[str]] = None,
        preprocess_defines: Optional[Mapping[str, str]] = None,
        preprocessed_sources: Optional[Mapping[str, str]] = None,
        low_memory: bool = False,
    ) -> "DesignIndex":
        for name, rec in list(merged.items()):
            if rec.stop_reason:
                continue
            if source_path_matches(rec.file_path, path_patterns):
                merged[name] = ModuleRecord(
                    module_name=rec.module_name,
                    file_path=rec.file_path,
                    stop_reason="ignorePath",
                )
                continue
            if _module_name_ignored(name, module_patterns):
                merged[name] = ModuleRecord(
                    module_name=rec.module_name,
                    file_path=rec.file_path,
                    stop_reason="ignorePath",
                )
        _inject_referenced_ignore_stubs(
            merged,
            path_patterns=path_patterns,
            module_patterns=module_patterns,
            filelist_patterns=list(filelist_patterns or ()),
        )
        if library_files is not None or library_dirs is not None:
            stubs = scan_library_modules(
                library_files or [],
                library_dirs or [],
                libexts=libexts or (),
                skip_path_patterns=path_patterns,
                jobs=index_jobs,
            )
            for name, stub in stubs.items():
                if name not in merged:
                    merged[name] = ModuleRecord(
                        module_name=stub.module_name,
                        file_path=stub.file_path,
                        is_blackbox=True,
                        stop_reason="ignorePath",
                    )
        index = cls(
            merged,
            ignore_path_patterns=path_patterns,
            ignore_module_patterns=module_patterns,
            ignore_filelist_patterns=list(filelist_patterns or ()),
            file_via_filelist=file_via_filelist,
            file_filelist_chain=file_filelist_chain,
            filelist_info=filelist_info,
            filelist_children=filelist_children,
            filelist_edges=filelist_edges,
            preprocess_include_dirs=preprocess_include_dirs,
            preprocess_defines=preprocess_defines,
            preprocessed_sources=preprocessed_sources,
            low_memory=low_memory,
        )
        index.index_jobs = index_jobs
        return index

    @classmethod
    def build_from_sources(
        cls,
        sources: Sequence[str],
        *,
        include_dirs: Sequence[str],
        defines: Mapping[str, str],
        library_files: Optional[List[str]] = None,
        library_dirs: Optional[List[str]] = None,
        libexts: Optional[List[str]] = None,
        ignore_paths: Optional[List[str]] = None,
        ignore_path_files: Optional[List[str]] = None,
        ignore_modules: Optional[List[str]] = None,
        ignore_filelists: Optional[List[str]] = None,
        jobs: int = 0,
        low_memory: bool = False,
        on_progress: Optional[Callable[[str], None]] = None,
        file_via_filelist: Optional[Mapping[str, str]] = None,
        file_filelist_chain: Optional[Mapping[str, str]] = None,
        filelist_info: Optional[Mapping[str, FilelistLinkInfo]] = None,
        filelist_children: Optional[Mapping[str, List[str]]] = None,
        filelist_edges: Optional[List[tuple[str, str, str]]] = None,
    ) -> "DesignIndex":
        path_patterns, module_patterns, filelist_patterns = resolve_ignore_path_patterns(
            ignore_paths or (),
            ignore_path_files=ignore_path_files or (),
            ignore_modules=ignore_modules or (),
            ignore_filelists=ignore_filelists or (),
        )
        src_list = sorted(
            {str(Path(s).resolve()) for s in sources},
            key=str,
        )
        parse_sources, ignore_sources = partition_sources(
            src_list,
            path_patterns,
            filelist_patterns=filelist_patterns,
            file_via_filelist=file_via_filelist,
            file_filelist_chain=file_filelist_chain,
        )
        if on_progress and ignore_sources:
            rule_bits: List[str] = []
            if path_patterns:
                rule_bits.append(f"{len(path_patterns)} path")
            if filelist_patterns:
                rule_bits.append(f"{len(filelist_patterns)} filelist")
            rules = " + ".join(rule_bits) if rule_bits else "ignore"
            on_progress(
                f"index: {len(ignore_sources)} sources ignored "
                f"({rules}; skip scan, stub on reference)"
            )
        merged = _build_merged_from_sources(
            parse_sources,
            ignore_sources,
            include_dirs=include_dirs,
            defines=defines,
            jobs=jobs,
            low_memory=low_memory,
            skip_path_patterns=path_patterns,
            on_progress=on_progress,
            file_via_filelist=file_via_filelist,
        )
        return cls._assemble(
            merged,
            path_patterns=path_patterns,
            module_patterns=module_patterns,
            filelist_patterns=filelist_patterns,
            library_files=library_files,
            library_dirs=library_dirs,
            libexts=libexts,
            file_via_filelist=file_via_filelist,
            file_filelist_chain=file_filelist_chain,
            filelist_info=filelist_info,
            filelist_children=filelist_children,
            filelist_edges=filelist_edges,
            index_jobs=_resolve_jobs(jobs, len(parse_sources) + len(ignore_sources)),
            preprocess_include_dirs=include_dirs,
            preprocess_defines=defines,
            low_memory=low_memory,
        )

    @classmethod
    def build(
        cls,
        preprocessed: Mapping[str, str],
        *,
        library_files: Optional[List[str]] = None,
        library_dirs: Optional[List[str]] = None,
        libexts: Optional[List[str]] = None,
        ignore_paths: Optional[List[str]] = None,
        ignore_path_files: Optional[List[str]] = None,
        ignore_modules: Optional[List[str]] = None,
        ignore_filelists: Optional[List[str]] = None,
        jobs: int = 0,
        on_progress: Optional[Callable[[str], None]] = None,
        file_via_filelist: Optional[Mapping[str, str]] = None,
        file_filelist_chain: Optional[Mapping[str, str]] = None,
        filelist_info: Optional[Mapping[str, FilelistLinkInfo]] = None,
        filelist_children: Optional[Mapping[str, List[str]]] = None,
        filelist_edges: Optional[List[tuple[str, str, str]]] = None,
    ) -> DesignIndex:
        path_patterns, module_patterns, filelist_patterns = resolve_ignore_path_patterns(
            ignore_paths or (),
            ignore_path_files=ignore_path_files or (),
            ignore_modules=ignore_modules or (),
            ignore_filelists=ignore_filelists or (),
        )
        sources = sorted(preprocessed.keys())
        parse_sources, ignore_sources = partition_sources(
            sources,
            path_patterns,
            filelist_patterns=filelist_patterns,
            file_via_filelist=file_via_filelist,
            file_filelist_chain=file_filelist_chain,
        )
        merged = _scan_sources(
            preprocessed,
            parse_sources,
            [],
            jobs=jobs,
            on_progress=on_progress,
        )
        return cls._assemble(
            merged,
            path_patterns=path_patterns,
            module_patterns=module_patterns,
            filelist_patterns=filelist_patterns,
            library_files=library_files,
            library_dirs=library_dirs,
            libexts=libexts,
            file_via_filelist=file_via_filelist,
            file_filelist_chain=file_filelist_chain,
            filelist_info=filelist_info,
            filelist_children=filelist_children,
            filelist_edges=filelist_edges,
            index_jobs=_resolve_jobs(jobs, len(parse_sources) + len(ignore_sources)),
            preprocessed_sources=preprocessed,
        )

    def get_module(self, name: str) -> Optional[ModuleRecord]:
        return self.modules.get(name)

    def filelist_for(self, rtl_file: str) -> str:
        key = str(Path(rtl_file).resolve()) if rtl_file else ""
        return self.file_via_filelist.get(key, "")

    def filelist_chain_for(self, rtl_file: str) -> str:
        key = str(Path(rtl_file).resolve()) if rtl_file else ""
        return self.file_filelist_chain.get(key, "")

    def module_stop_reason(self, mod_name: str) -> str:
        rec = self.modules.get(mod_name)
        if rec is None:
            return "unknown"
        if rec.stop_reason:
            return rec.stop_reason
        if _module_name_ignored(mod_name, self.ignore_module_patterns):
            return "ignorePath"
        if rec.is_blackbox:
            return "ignorePath"
        return ""

    def instances_for_walk(
        self,
        mod_name: str,
        parent_ctx: Mapping[str, str],
    ) -> List[InstanceEdge]:
        """
        Path-walk instance edges: tier-1 prescanned insts plus folded generate bodies.

        Tier-1 already records instances declared outside ``generate`` even when
        ``needs_generate_fold`` is set; do not drop them in favour of a full
        ``instances_for`` rescan (slow preprocess + possible ifdef divergence).
        """
        rec = self.modules.get(mod_name)
        if not rec or rec.stop_reason or rec.is_blackbox:
            return list(rec.instances) if rec else []
        if not rec.needs_generate_fold:
            return list(rec.instances)
        base = list(rec.instances)
        folded = self.instances_for(mod_name, parent_ctx, {})
        if not folded:
            return base
        seen = {edge.inst_name for edge in base}
        out = list(base)
        for edge in folded:
            if edge.inst_name not in seen:
                out.append(edge)
                seen.add(edge.inst_name)
        return out

    def instances_for(
        self,
        mod_name: str,
        parent_ctx: Mapping[str, str],
        overrides: Mapping[str, str],
    ) -> List[InstanceEdge]:
        rec = self.modules.get(mod_name)
        if not rec:
            return []
        if rec.stop_reason or rec.is_blackbox:
            return list(rec.instances)

        if (
            not rec.needs_generate_fold
            and not overrides
            and not parent_ctx
        ):
            return list(rec.instances)

        if not overrides and not rec.needs_generate_fold:
            pmap = resolve_param_map(
                rec.raw_params,
                overrides=overrides,
                parent=parent_ctx,
            )
            if _ctx_key(pmap) == self._default_ctx.get(mod_name):
                return rec.instances

        body = self.module_body(mod_name)
        if not body and rec.instances:
            return list(rec.instances)

        raw_params = rec.raw_params
        if rec.needs_generate_fold:
            text = self._source_text(rec.file_path, full=True)
            hdr, full_body = _module_header_body(text, mod_name)
            if full_body:
                body = full_body
                raw_params = collect_module_params(hdr, full_body, max_body_bytes=0)

        pmap = resolve_param_map(
            raw_params,
            overrides=overrides,
            parent=parent_ctx,
        )
        fold_ctx = dict(self._preprocess_defines)
        fold_ctx.update(pmap)
        ctx_key = _ctx_key(fold_ctx)
        if not overrides and ctx_key == self._default_ctx.get(mod_name):
            return rec.instances

        cache_key = (mod_name, ctx_key)
        with self._instance_cache_lock:
            cached = self._instance_cache.get(cache_key)
            if cached is not None:
                return cached

        edges = _scan_module_body(
            body,
            raw_params,
            parent_ctx=parent_ctx,
            overrides=overrides,
            compile_defines=self._preprocess_defines,
        )
        with self._instance_cache_lock:
            hit = self._instance_cache.get(cache_key)
            if hit is not None:
                return hit
            self._instance_cache[cache_key] = edges
            return edges