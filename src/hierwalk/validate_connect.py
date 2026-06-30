"""Cross-validate connectivity without rebuilding the full design index.

Production runs use lazy index + scoped elab. This module checks the same
connect JSON against stricter oracles on the *same* or *surgically patched*
index so large SoCs can be validated without HIERWALK_LAZY=0 on every file.
"""

from __future__ import annotations

import argparse
import copy
import os
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from hierwalk.connect_endpoints import parse_connect_endpoint
from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest, load_connect_request
from hierwalk.connectivity import ConnectResult, ConnectivityBatchResult, run_connectivity_request
from hierwalk.elab import elaborate
from hierwalk.filelist import parse_filelist
from hierwalk.index import DesignIndex
from hierwalk.lazy_scope import elab_scope_paths, endpoint_specs_from_request, lazy_scoped_connect_elab
from hierwalk.models import FlatRow
from hierwalk.progress import progress_callback
from hierwalk.run_request import apply_config_env_from_document, jobs_from_env


class OracleKind(str, Enum):
    STRICT = "strict"
    EAGER_FILES = "eager-files"


@dataclass(frozen=True)
class CheckOracleDiff:
    check_id: str
    endpoint_a: str
    endpoint_b: str
    production: bool
    oracle: bool
    oracle_kind: OracleKind
    risk: str
    production_note: str = ""
    oracle_note: str = ""


@dataclass
class ConnectValidationReport:
    production: ConnectivityBatchResult
    oracle_kind: OracleKind
    oracle_results: ConnectivityBatchResult
    diffs: List[CheckOracleDiff] = field(default_factory=list)
    endpoint_files: Tuple[str, ...] = ()
    production_sec: float = 0.0
    oracle_sec: float = 0.0
    eager_patch_sec: float = 0.0

    @property
    def total_checks(self) -> int:
        return len(self.production.results)

    @property
    def mismatch_count(self) -> int:
        return len(self.diffs)

    @property
    def ok(self) -> bool:
        return not self.diffs


def _rows_by_path(rows: Sequence[FlatRow]) -> Dict[str, FlatRow]:
    return {r.full_path: r for r in rows}


def files_for_endpoint_specs(
    rows: Sequence[FlatRow],
    specs: Sequence[str],
) -> Set[str]:
    """RTL source files touched by endpoint hierarchy prefixes."""
    by_path = _rows_by_path(rows)
    files: Set[str] = set()
    for raw in specs:
        spec = str(raw).strip()
        if not spec:
            continue
        parts = spec.split(".")
        for i in range(1, len(parts) + 1):
            prefix = ".".join(parts[:i])
            row = by_path.get(prefix)
            if row is not None and row.file:
                files.add(row.file)
        hier, _port = parse_connect_endpoint(spec, by_path)
        row = by_path.get(hier)
        if row is not None and row.file:
            files.add(row.file)
    return files


def _result_key(chk: ConnectivityCheck, result: ConnectResult) -> Tuple[str, str, str]:
    cid = chk.check_id or result.check_id
    return (cid, chk.endpoint_a, chk.endpoint_b)


def _index_results(
    request: ConnectivityRequest,
    batch: ConnectivityBatchResult,
) -> Dict[Tuple[str, str, str], ConnectResult]:
    out: Dict[Tuple[str, str, str], ConnectResult] = {}
    for chk, res in zip(request.checks, batch.results):
        out[_result_key(chk, res)] = res
    return out


def _classify_diff(
    production: bool,
    oracle: bool,
    *,
    oracle_kind: OracleKind,
) -> str:
    if production == oracle:
        return "ok"
    if production and not oracle:
        return "fp_risk"
    return "fn_risk"


def compare_connect_batches(
    request: ConnectivityRequest,
    production: ConnectivityBatchResult,
    oracle: ConnectivityBatchResult,
    *,
    oracle_kind: OracleKind,
) -> List[CheckOracleDiff]:
    prod_map = _index_results(request, production)
    oracle_map = _index_results(request, oracle)
    diffs: List[CheckOracleDiff] = []
    for chk in request.checks:
        key = (chk.check_id, chk.endpoint_a, chk.endpoint_b)
        p = prod_map.get(key)
        o = oracle_map.get(key)
        if p is None or o is None:
            continue
        risk = _classify_diff(p.connected, o.connected, oracle_kind=oracle_kind)
        if risk == "ok":
            continue
        diffs.append(
            CheckOracleDiff(
                check_id=chk.check_id,
                endpoint_a=chk.endpoint_a,
                endpoint_b=chk.endpoint_b,
                production=p.connected,
                oracle=o.connected,
                oracle_kind=oracle_kind,
                risk=risk,
                production_note=p.note or "",
                oracle_note=o.note or "",
            )
        )
    return diffs


def _strict_request(request: ConnectivityRequest) -> ConnectivityRequest:
    from dataclasses import replace

    return replace(
        request,
        strict_generate=True,
        over_approximate_if=False,
    )


def _run_strict_oracle(
    request: ConnectivityRequest,
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    extra_defines: Mapping[str, str] | None,
    jobs: int,
) -> ConnectivityBatchResult:
    return run_connectivity_request(
        _strict_request(request),
        rows=rows,
        index=index,
        top=top,
        extra_defines=extra_defines,
        jobs=jobs,
    )


def _run_eager_files_oracle(
    request: ConnectivityRequest,
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    extra_defines: Mapping[str, str] | None,
    endpoint_files: Sequence[str],
    include_dirs: Sequence[str],
    defines: Mapping[str, str],
    jobs: int,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Tuple[DesignIndex, List[FlatRow], ConnectivityBatchResult, float]:
    """Re-preprocess only endpoint RTL with eager index settings, then re-elab."""
    t0 = time.perf_counter()
    eager_index = copy.deepcopy(index)
    prev_lazy = os.environ.get("HIERWALK_LAZY")
    os.environ["HIERWALK_LAZY"] = "0"
    try:
        eager_index.patch_files(
            list(endpoint_files),
            [],
            include_dirs=include_dirs,
            defines=defines,
            jobs=jobs,
            on_progress=on_progress,
        )
    finally:
        if prev_lazy is None:
            os.environ.pop("HIERWALK_LAZY", None)
        else:
            os.environ["HIERWALK_LAZY"] = prev_lazy
    patch_sec = time.perf_counter() - t0

    specs = endpoint_specs_from_request(request)
    scope = elab_scope_paths(specs, top=top) if specs else None
    _root, eager_rows = elaborate(
        eager_index,
        top,
        scope_paths=scope,
    )
    batch = run_connectivity_request(
        request,
        rows=eager_rows,
        index=eager_index,
        top=top,
        extra_defines=extra_defines,
        jobs=jobs,
    )
    return eager_index, eager_rows, batch, patch_sec


def waypoint_perf_warnings(request: ConnectivityRequest) -> List[str]:
    """Flag waypoint-fanout checks whose BFS cost multiplies at scale."""
    out: List[str] = []
    for chk in request.checks:
        expand = chk.expand
        if expand is None or expand.map_kind != "waypoint-fanout":
            continue
        kinds = len(expand.path_kinds)
        dual = getattr(expand, "direction", "fanout") == "both"
        passes = kinds * (2 if dual else 1)
        label = chk.check_id or chk.endpoint_a
        if passes >= 4:
            out.append(
                f"check {label!r}: waypoint-fanout runs up to {passes} full cone "
                f"BFS passes (direction={expand.direction}, "
                f"path_kind={list(expand.path_kinds)}); prefer a single path_kind "
                f"for batch verification"
            )
        elif passes >= 2:
            out.append(
                f"check {label!r}: waypoint-fanout runs {passes} cone BFS passes "
                f"(direction={expand.direction}, path_kind={list(expand.path_kinds)})"
            )
    return out


def validate_connect_request(
    request: ConnectivityRequest,
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    extra_defines: Mapping[str, str] | None = None,
    oracle: OracleKind = OracleKind.STRICT,
    include_dirs: Sequence[str] = (),
    preprocess_defines: Mapping[str, str] | None = None,
    jobs: int = 0,
    on_progress: Optional[Callable[[str], None]] = None,
) -> ConnectValidationReport:
    """
    Run production connect checks, then compare against an oracle profile.

    * **strict** — same index/elab; ``strict_generate`` + no ``if`` over-approx.
    * **eager-files** — eager preprocess on endpoint RTL files only, then re-elab.
    """
    perf_notes = waypoint_perf_warnings(request)
    if perf_notes and on_progress:
        for note in perf_notes:
            on_progress(f"validate: perf note: {note}")
    t0 = time.perf_counter()
    production = run_connectivity_request(
        request,
        rows=rows,
        index=index,
        top=top,
        extra_defines=extra_defines,
        jobs=jobs,
    )
    prod_sec = time.perf_counter() - t0

    specs = endpoint_specs_from_request(request)
    endpoint_files = tuple(sorted(files_for_endpoint_specs(rows, specs)))

    t1 = time.perf_counter()
    eager_patch_sec = 0.0
    if oracle == OracleKind.STRICT:
        oracle_batch = _run_strict_oracle(
            request,
            rows=rows,
            index=index,
            top=top,
            extra_defines=extra_defines,
            jobs=jobs,
        )
    elif oracle == OracleKind.EAGER_FILES:
        if not endpoint_files:
            if on_progress:
                on_progress("validate: no endpoint RTL files resolved; skip eager-files")
            oracle_batch = production
        else:
            if on_progress:
                on_progress(
                    f"validate: eager preprocess {len(endpoint_files)} endpoint file(s)"
                )
            _eidx, _erows, oracle_batch, eager_patch_sec = _run_eager_files_oracle(
                request,
                rows=rows,
                index=index,
                top=top,
                extra_defines=extra_defines,
                endpoint_files=endpoint_files,
                include_dirs=include_dirs,
                defines=dict(preprocess_defines or {}),
                jobs=jobs,
                on_progress=on_progress,
            )
    else:
        raise ValueError(f"unknown oracle: {oracle}")
    oracle_sec = time.perf_counter() - t1

    diffs = compare_connect_batches(
        request,
        production,
        oracle_batch,
        oracle_kind=oracle,
    )
    return ConnectValidationReport(
        production=production,
        oracle_kind=oracle,
        oracle_results=oracle_batch,
        diffs=diffs,
        endpoint_files=endpoint_files,
        production_sec=prod_sec,
        oracle_sec=oracle_sec,
        eager_patch_sec=eager_patch_sec,
    )


def format_validation_report(report: ConnectValidationReport) -> str:
    lines = [
        "connect validation",
        f"  checks:          {report.total_checks}",
        f"  oracle:          {report.oracle_kind.value}",
        f"  mismatches:      {report.mismatch_count}",
        f"  endpoint files:  {len(report.endpoint_files)}",
        (
            "  timing:          "
            f"production {report.production_sec:.2f}s, "
            f"oracle {report.oracle_sec:.2f}s"
            + (
                f", eager-patch {report.eager_patch_sec:.2f}s"
                if report.eager_patch_sec > 0
                else ""
            )
        ),
    ]
    if report.endpoint_files:
        lines.append("")
        lines.append("Endpoint RTL (eager-files oracle)")
        for fpath in report.endpoint_files:
            lines.append(f"  {fpath}")
    if report.diffs:
        lines.append("")
        lines.append("Mismatches (production vs oracle)")
        lines.append("  check_id\trisk\tproduction\toracle\tendpoint_a\tendpoint_b")
        for d in report.diffs:
            lines.append(
                f"  {d.check_id or '-'}\t{d.risk}\t{d.production}\t{d.oracle}\t"
                f"{d.endpoint_a}\t{d.endpoint_b}"
            )
    else:
        lines.append("")
        lines.append("OK — production matches oracle")
    return "\n".join(lines) + "\n"


def _load_connect_with_env(path: Path) -> ConnectivityRequest:
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, Mapping):
        apply_config_env_from_document(data, overwrite=True)
    return load_connect_request(path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    from hierwalk.cache import default_cache_dir, load_or_build_index
    from hierwalk.elab import elaborate_tops_parallel
    from hierwalk.lazy_scope import lazy_filelist_defer_exists
    from hierwalk.perf import effective_low_memory
    from hierwalk.progress import ProgressReporter
    from hierwalk.top_find import resolve_top_modules

    ap = argparse.ArgumentParser(
        description=(
            "Validate connect results against a stricter oracle "
            "(no full eager index required)"
        )
    )
    ap.add_argument("filelist", help="Top filelist (.f)")
    ap.add_argument("--connect", required=True, help="Connectivity batch JSON")
    ap.add_argument("--top", default="", help="Elaboration top")
    ap.add_argument("--index-cwd", default="", help="Filelist index cwd")
    ap.add_argument(
        "--oracle",
        choices=[o.value for o in OracleKind],
        default=OracleKind.STRICT.value,
        help="strict: same elab, stricter connect; eager-files: eager preprocess on endpoint RTL only",
    )
    ap.add_argument("--jobs", type=int, default=0, help="Parallel workers (0=auto)")
    ap.add_argument("--no-cache", action="store_true", help="Skip index cache")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    connect_path = Path(args.connect).resolve()
    request = _load_connect_with_env(connect_path)
    top = args.top or request.top
    jobs = args.jobs if args.jobs != 0 else jobs_from_env()

    reporter = ProgressReporter(enabled=not args.quiet)
    on_progress = progress_callback(reporter)

    fl = parse_filelist(
        args.filelist,
        index_cwd=args.index_cwd or None,
        extra_defines=request.defines,
        on_progress=on_progress,
        defer_source_exists=lazy_filelist_defer_exists(),
    )
    if not fl.source_files:
        print("No sources in filelist", file=sys.stderr)
        return 1

    defines = dict(fl.defines)
    defines.update(request.defines)

    low_memory = effective_low_memory(explicit=False, num_sources=len(fl.source_files))
    index, _bundle, _hit, _rebuilt, _incr, _cpath = load_or_build_index(
        args.filelist,
        fl,
        cache_dir=default_cache_dir(),
        extra_defines=defines,
        ignore_paths=(),
        ignore_path_files=(),
        ignore_modules=(),
        ignore_filelists=(),
        jobs=jobs,
        use_cache=not args.no_cache,
        refresh_cache=False,
        low_memory=low_memory,
        on_progress=on_progress,
    )

    tops = resolve_top_modules(index, top=top, filelist_tops=fl.top_modules, all_tops=False)
    top_name = tops[0]
    specs = endpoint_specs_from_request(request)
    elab_scope = None
    if lazy_scoped_connect_elab() and specs:
        elab_scope = elab_scope_paths(specs, top=top_name)
        if on_progress:
            on_progress(f"validate: scoped elab {len(elab_scope)} path(s)")

    _roots, rows, _hits = elaborate_tops_parallel(
        index,
        tops,
        scope_paths=elab_scope,
        jobs=jobs,
        on_progress=on_progress,
    )

    report = validate_connect_request(
        request,
        rows=rows,
        index=index,
        top=top_name,
        extra_defines=defines,
        oracle=OracleKind(args.oracle),
        include_dirs=fl.include_dirs,
        preprocess_defines=defines,
        jobs=jobs,
        on_progress=on_progress,
    )
    sys.stdout.write(format_validation_report(report))
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())