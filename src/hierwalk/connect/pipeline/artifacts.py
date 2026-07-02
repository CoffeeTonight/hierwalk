"""Two-phase connect artifacts (text-conn / logical-conn) under the per-run db folder."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

from hierwalk.connect.session import (
    ConnectivitySession,
    flatten_connect_results,
    flatten_connect_results_for_output,
    format_connect_results_tsv,
)
from hierwalk.connect.shared.expand import (
    _LOOP_PLACEHOLDER_RE,
    _expand_looped_endpoint,
    aggregate_connect_results,
    expand_check_to_pairs,
    hierarchy_endpoint_specs,
    parse_endpoint_elements,
)
from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.hierarchy_log import path_spine_prefixes
from hierwalk.index import DesignIndex
from hierwalk.models import ConnectEndpoint, ConnectResult, FlatRow
from hierwalk.run_request import RunConfig

HIERARCHY_KINDS = frozenset({"inst", "port", "wire", "reg"})


@dataclass(frozen=True)
class HierarchyRowContext:
    """Maps walked paths to (check_id, side) for per-line hierarchy TSV rows."""

    pairs: Tuple[Tuple[str, frozenset[str], frozenset[str]], ...]


def _prefixes_for_endpoint_value(spec: object) -> frozenset[str]:
    _display, elements, _is_list, _is_concat = parse_endpoint_elements(spec)
    out: set[str] = set()
    for el in elements:
        text = str(el).strip()
        if not text:
            continue
        for spec_path in hierarchy_endpoint_specs(text):
            out.add(spec_path)
            for prefix in path_spine_prefixes(spec_path):
                out.add(prefix)
    return frozenset(out)


def build_hierarchy_row_context(chk: ConnectivityCheck) -> HierarchyRowContext:
    pairs_out: List[Tuple[str, frozenset[str], frozenset[str]]] = []
    for pair in expand_check_to_pairs(
        chk.endpoint_a,
        chk.endpoint_b,
        check_id=chk.check_id,
        expand=chk.expand,
    ):
        sub_id = (
            f"{chk.check_id}{pair.sub_id}"
            if chk.check_id and pair.sub_id
            else (chk.check_id or pair.sub_id.strip("[]->") or "-")
        )
        pairs_out.append(
            (
                sub_id,
                _prefixes_for_endpoint_value(pair.endpoint_a),
                _prefixes_for_endpoint_value(pair.endpoint_b),
            )
        )
    if not pairs_out:
        pairs_out.append(
            (
                chk.check_id or "-",
                _prefixes_for_endpoint_value(chk.endpoint_a),
                _prefixes_for_endpoint_value(chk.endpoint_b),
            )
        )
    return HierarchyRowContext(tuple(pairs_out))


def resolve_hierarchy_row_identity(
    ctx: HierarchyRowContext,
    path: str,
) -> Tuple[str, str]:
    text = (path or "").strip()
    if not text or not ctx.pairs:
        return ("", "-")
    best: Optional[Tuple[str, str]] = None
    best_len = -1
    for check_id, a_prefs, b_prefs in ctx.pairs:
        for side, prefs in (("a", a_prefs), ("b", b_prefs)):
            for pref in prefs:
                if text == pref or text.startswith(pref + "."):
                    if len(pref) > best_len:
                        best_len = len(pref)
                        best = (check_id, side)
    if best is not None:
        return best
    return (ctx.pairs[0][0], "-")


@dataclass(frozen=True)
class HierarchyEvidenceRow:
    """One resolved hierarchy element for a connect check (inst/port/wire/reg)."""

    check_id: str
    side: str
    kind: str
    path: str
    status: str
    module: str = ""
    rtl: str = ""
    via_filelist: str = ""
    filelist_chain: str = ""


@dataclass(frozen=True)
class SignalTailRecord:
    """Path-walk wire/port tail resolution (mirrors signal-tail trace log)."""

    target_path: str
    parent_path: str
    tail: str
    kind: str
    hit: bool
    module: str = ""


@dataclass(frozen=True)
class ConnectOutputPaths:
    text_tsv: Path
    logical_tsv: Path
    hierarchy_text_tsv: Path
    hierarchy_logical_tsv: Path


def default_verification_artifact_name(kind: str) -> str:
    """Default logical TSV basename per flat-suite verification block (text adds ``.text``)."""
    key = (kind or "").strip().lower().replace("-", "_")
    if key == "run_conn_check":
        return "conn.tsv"
    if key == "run_io_trace":
        return "io_trace.tsv"
    if key == "run_cone_trace":
        return "cone_trace.tsv"
    if key in ("run_on_full_index", "run_on_full_db"):
        return "instances.tsv"
    return "output.tsv"


def artifact_output_basename(output: str, *, default: str = "output.tsv") -> str:
    """Basename for an artifact file stored under the per-top work directory."""
    if not output or output == "-":
        return default
    return Path(output).name


def connect_output_basename(output: str = "conn.tsv") -> str:
    """Filename for logical connect TSV (text phase uses ``.text`` suffix)."""
    return artifact_output_basename(output, default="conn.tsv")


def hierarchy_output_basename(output: str = "conn.tsv") -> str:
    """Hierarchy TSV basename paired with a connect output name."""
    logical_name = connect_output_basename(output)
    stem = Path(logical_name).stem
    suffix = Path(logical_name).suffix or ".tsv"
    if stem.endswith("_conn"):
        hier_stem = f"{stem[:-5]}_hierarchy"
    elif stem == "conn":
        hier_stem = "hierarchy"
    else:
        hier_stem = f"{stem}_hierarchy"
    return f"{hier_stem}{suffix}"


def work_dir_artifact_path(
    work_dir: Path,
    output: str,
    *,
    phase: str = "logical",
    default: str = "output.tsv",
) -> Path:
    """Resolve a run output path under ``.db_{TOP}/`` (never beside JSON/filelist)."""
    root = work_dir.expanduser().resolve()
    logical_name = artifact_output_basename(output, default=default)
    if str(phase).strip().lower() == "text":
        filename = verification_output_path(logical_name, "text").name
    else:
        filename = logical_name
    return root / filename


def work_dir_sidecar_path(work_dir: Path, path: Optional[str]) -> Optional[Path]:
    """Resolve auxiliary artifacts (logs, dot graphs) under the work directory."""
    if not path:
        return None
    return work_dir.expanduser().resolve() / Path(path).name


def connect_output_paths(
    work_dir: Path,
    output: str = "conn.tsv",
) -> ConnectOutputPaths:
    root = work_dir.expanduser().resolve()
    logical_name = connect_output_basename(output)
    text_name = verification_output_path(logical_name, "text").name
    hier_logical = hierarchy_output_basename(output)
    hier_text = verification_output_path(hier_logical, "text").name
    return ConnectOutputPaths(
        text_tsv=root / text_name,
        logical_tsv=root / logical_name,
        hierarchy_text_tsv=root / hier_text,
        hierarchy_logical_tsv=root / hier_logical,
    )


def resolve_connect_work_dir(
    cfg: RunConfig,
    *,
    top: str = "",
) -> Path:
    from hierwalk.cache import resolve_run_work_dir, work_base_dir

    return resolve_run_work_dir(
        top or cfg.top or "top",
        base=work_base_dir(cfg.index_cwd),
        explicit_cache_dir=cfg.cache_dir,
    )


def connect_artifact_paths(cfg: RunConfig, *, top: str = "") -> ConnectOutputPaths:
    """Resolved text/logical connect TSV paths for a run config."""
    return connect_output_paths(resolve_connect_work_dir(cfg, top=top), cfg.output)


def format_connect_artifact_log(cfg: RunConfig, *, top: str = "") -> str:
    """Human-readable artifact path(s) for path-walk connect under the work dir."""
    paths = connect_artifact_paths(cfg, top=top)
    phase = (cfg.verification_phase or "both").strip().lower()
    if phase == "text":
        return str(paths.text_tsv.resolve())
    if phase == "logical":
        return str(paths.logical_tsv.resolve())
    return f"{paths.text_tsv.resolve()}, {paths.logical_tsv.resolve()}"


def format_verification_artifact_log(cfg: RunConfig, *, top: str = "") -> str:
    """Resolved artifact path(s) for any verification step under the work dir."""
    from hierwalk.run_request import RUN_CONN_CHECK, normalize_run_mode

    if (
        cfg.verification_step_kind == RUN_CONN_CHECK
        or normalize_run_mode(cfg.mode or "") in ("check-connect", "check-connect-batch", "path-walk")
    ):
        return format_connect_artifact_log(cfg, top=top)
    work = resolve_connect_work_dir(cfg, top=top)
    phase = (cfg.verification_phase or "both").strip().lower()
    if phase == "text":
        return str(work_dir_artifact_path(work, cfg.output, phase="text").resolve())
    if phase == "logical":
        return str(work_dir_artifact_path(work, cfg.output, phase="logical").resolve())
    text_p = work_dir_artifact_path(work, cfg.output, phase="text")
    logical_p = work_dir_artifact_path(work, cfg.output, phase="logical")
    return f"{text_p.resolve()}, {logical_p.resolve()}"


def format_connect_artifact_help(cfg: RunConfig, *, top: str = "") -> str:
    """One-line hint: work dir + text/logical filenames (no conn.logical.tsv)."""
    work = resolve_connect_work_dir(cfg, top=top)
    paths = connect_output_paths(work, cfg.output)
    return (
        f"work-dir={work.resolve()} "
        f"text={paths.text_tsv.name} logical={paths.logical_tsv.name}"
    )


def resolve_connect_output_dir(
    connect_output_dir: Optional[Path],
    *,
    top: str = "",
    base: Optional[Path] = None,
    cache_dir: Optional[Path] = None,
) -> Path:
    """
    Resolve the per-run db folder for connect phase TSV artifacts.

    Falls back through explicit *connect_output_dir*, the active work dir,
    then ``.db_{TOP}`` under *base* (or the parent of a ``.db_*`` *cache_dir*).
    """
    if connect_output_dir is not None:
        root = connect_output_dir.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root
    from hierwalk.cache import ensure_top_work_dir, get_active_work_dir, work_base_dir

    resolved_base = base
    if cache_dir is not None:
        cache_root = cache_dir.expanduser().resolve()
        if cache_root.name.startswith(".db_"):
            cache_root.mkdir(parents=True, exist_ok=True)
            return cache_root
        resolved_base = cache_root
    if resolved_base is None:
        active = get_active_work_dir()
        if active is not None:
            return active
        resolved_base = work_base_dir()
    return ensure_top_work_dir(top or "top", base=resolved_base)


def ensure_connect_phase_tsv(
    work_dir: Path,
    results: Sequence[ConnectResult],
    *,
    phase: str,
    output: str = "conn.tsv",
    modules_cached: Optional[int] = None,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
) -> Path:
    """Write phase TSV; always overwrites with *results* when invoked."""
    paths = connect_output_paths(work_dir, output)
    target = paths.text_tsv if phase == "text" else paths.logical_tsv
    return require_connect_phase_tsv(
        target,
        results,
        phase=phase,
        modules_cached=modules_cached,
        rows_by_path=rows_by_path,
    )


def expected_verification_artifact_paths(
    cfg: RunConfig,
    work_dir: Path,
) -> List[Path]:
    """Artifact files that must exist after a verification step completes."""
    from hierwalk.run_request import RUN_CONN_CHECK, RUN_IO_TRACE, RUN_CONE_TRACE
    from hierwalk.run_request import normalize_run_mode

    phase = (cfg.verification_phase or "both").strip().lower()
    paths: List[Path] = []
    is_connect = (
        cfg.verification_step_kind == RUN_CONN_CHECK
        or normalize_run_mode(cfg.mode or "") in (
            "check-connect",
            "check-connect-batch",
            "path-walk",
        )
    )
    if is_connect:
        conn_paths = connect_output_paths(work_dir, cfg.output)
        if phase in ("text", "both"):
            paths.append(conn_paths.text_tsv)
        if phase in ("logical", "both"):
            paths.append(conn_paths.logical_tsv)
        return paths
    if cfg.verification_step_kind in (RUN_IO_TRACE, RUN_CONE_TRACE):
        if phase in ("text", "both"):
            paths.append(work_dir_artifact_path(work_dir, cfg.output, phase="text"))
        if phase in ("logical", "both"):
            paths.append(work_dir_artifact_path(work_dir, cfg.output, phase="logical"))
    return paths


def missing_verification_artifacts(
    cfg: RunConfig,
    work_dir: Path,
) -> List[Path]:
    return [
        path
        for path in expected_verification_artifact_paths(cfg, work_dir)
        if not path.is_file()
    ]


def reorder_connect_checks_by_b_endpoint(
    request: ConnectivityRequest,
) -> ConnectivityRequest:
    """Group checks with the same B endpoint together (better walk/COI cache reuse)."""
    ordered = sorted(
        request.checks,
        key=lambda chk: (str(chk.endpoint_b), str(chk.endpoint_a), chk.check_id),
    )
    if ordered == list(request.checks):
        return request
    from dataclasses import replace

    return replace(request, checks=tuple(ordered))


def prepare_text_connect_request(
    request: ConnectivityRequest,
) -> ConnectivityRequest:
    """Text-conn batch prep: stable B grouping for shared endpoint walk."""
    return reorder_connect_checks_by_b_endpoint(request)


def verification_output_path(path: Union[str, Path], phase: str) -> Path:
    """Map configured output to text (.text.tsv) or logical (original) artifact path."""
    p = Path(path)
    if str(phase).strip().lower() == "text":
        if p.suffix:
            return p.with_name(f"{p.stem}.text{p.suffix}")
        return Path(f"{p}.text.tsv")
    return p.expanduser().resolve()


def archive_run_config_sources(work_dir: Path, cfg: RunConfig) -> List[Path]:
    """Copy RUN / connect-batch JSON used for this run into the db work dir."""
    root = work_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    archived: List[Path] = []
    seen: set[str] = set()
    candidates: List[Path] = []
    if cfg.run_config_source:
        candidates.append(Path(cfg.run_config_source))
    if cfg.check_connect_batch:
        candidates.append(Path(cfg.check_connect_batch))
    for src in candidates:
        key = str(src.expanduser().resolve())
        if key in seen or not src.is_file():
            continue
        seen.add(key)
        dest = root / src.name
        shutil.copy2(src, dest)
        archived.append(dest)
    return archived


def _walk_notes_for_endpoint(
    ep: ConnectEndpoint,
    rows_by_path: Mapping[str, FlatRow],
) -> List[str]:
    from hierwalk.lazy_scope import hierarchy_prefixes

    notes: List[str] = []
    inst = (ep.inst_path or "").strip()
    if not inst:
        return notes
    for prefix in hierarchy_prefixes([inst]):
        row = rows_by_path.get(prefix)
        if row is None:
            continue
        if row.refine_status == "provisional":
            notes.append(
                f"{prefix}: provisional walk (generate/array); refine pending"
            )
        elif row.refine_status == "inactive_ifdef":
            notes.append(f"{prefix}: inactive under current defines (`ifdef` gated)")
        elif row.activation == "inactive":
            notes.append(f"{prefix}: inactive under current defines")
    return notes


def _walk_notes_for_result(
    result: ConnectResult,
    rows_by_path: Mapping[str, FlatRow],
) -> List[str]:
    notes: List[str] = []
    for ep in (result.endpoint_a, result.endpoint_b):
        notes.extend(_walk_notes_for_endpoint(ep, rows_by_path))
    return list(dict.fromkeys(notes))


def _endpoints_have_inactive_ifdef(
    result: ConnectResult,
    rows_by_path: Mapping[str, FlatRow],
) -> bool:
    from hierwalk.lazy_scope import hierarchy_prefixes

    for ep in (result.endpoint_a, result.endpoint_b):
        inst = (ep.inst_path or "").strip()
        if not inst:
            continue
        for prefix in hierarchy_prefixes([inst]):
            row = rows_by_path.get(prefix)
            if row is None:
                continue
            if row.refine_status == "inactive_ifdef" or row.activation == "inactive":
                return True
    return False


def _endpoints_have_provisional(
    result: ConnectResult,
    rows_by_path: Mapping[str, FlatRow],
) -> bool:
    from hierwalk.lazy_scope import hierarchy_prefixes

    for ep in (result.endpoint_a, result.endpoint_b):
        inst = (ep.inst_path or "").strip()
        if not inst:
            continue
        for prefix in hierarchy_prefixes([inst]):
            row = rows_by_path.get(prefix)
            if row is None:
                continue
            if row.refine_status == "provisional":
                return True
    return False


def snapshot_connect_text_phase(results: Sequence[ConnectResult]) -> None:
    """Record structural/text COI outcome before logical post-processing."""
    for result in flatten_connect_results(results):
        result.connected_text = result.connected


def flatten_text_conn_results(results: Sequence[ConnectResult]) -> List[ConnectResult]:
    return flatten_connect_results(results)


def build_connect_results_from_request(
    request: ConnectivityRequest,
    session: ConnectivitySession,
    *,
    coi_error: str = "",
) -> List[ConnectResult]:
    """Synthesize per-check rows from JSON endpoints when COI produced none."""
    from hierwalk.connect.shared.endpoints import resolve_endpoint

    lookup = session.rows_by_path
    out: List[ConnectResult] = []
    for chk in request.checks:
        pairs = expand_check_to_pairs(
            chk.endpoint_a,
            chk.endpoint_b,
            check_id=chk.check_id,
            expand=chk.expand,
        )
        fanout_mode = chk.expand.fanout_mode if chk.expand is not None else "all"
        sub_results: List[ConnectResult] = []
        for pair in pairs:
            ep_a, err_a = resolve_endpoint(
                pair.endpoint_a,
                session.rows,
                session.index,
                top=session.top,
                require_port=False,
                rows_by_path=lookup,
            )
            ep_b, err_b = resolve_endpoint(
                pair.endpoint_b,
                session.rows,
                session.index,
                top=session.top,
                require_port=False,
                rows_by_path=lookup,
            )
            errors = list(err_a) + list(err_b)
            if coi_error and coi_error not in errors:
                errors.insert(0, coi_error)
            sub_id = pair.sub_id
            leaf_id = (
                f"{chk.check_id}{sub_id}"
                if chk.check_id and sub_id
                else (chk.check_id or sub_id.strip("[]->"))
            )
            sub_results.append(
                ConnectResult(
                    ep_a,
                    ep_b,
                    False,
                    "unknown",
                    errors=errors,
                    check_id=leaf_id,
                )
            )
        if len(sub_results) == 1:
            out.append(sub_results[0])
        elif len(sub_results) > 1:
            out.append(
                aggregate_connect_results(
                    chk.endpoint_a,
                    chk.endpoint_b,
                    sub_results,
                    check_id=chk.check_id,
                    fanout_mode=fanout_mode,
                )
            )
    return out


def normalize_connect_results(
    request: ConnectivityRequest,
    results: Sequence[ConnectResult],
    session: ConnectivitySession,
    *,
    coi_error: str = "",
) -> List[ConnectResult]:
    """Ensure TSV/report list at least one row per request check."""
    if flatten_connect_results_for_output(results):
        return list(results)
    if not request.checks:
        return []
    return build_connect_results_from_request(
        request,
        session,
        coi_error=coi_error,
    )


def any_text_conn_hit(results: Sequence[ConnectResult]) -> bool:
    """True when at least one leaf check passed text-conn (worth a logical pass)."""
    return any(r.connected_text for r in flatten_text_conn_results(results))


def _leaf_passed_text_conn(leaf: ConnectResult) -> bool:
    if leaf.connected_text is not None:
        return bool(leaf.connected_text)
    return bool(leaf.connected)


def _build_text_connect_lookup(
    text_results: Sequence[ConnectResult],
) -> Tuple[Dict[str, ConnectResult], Dict[Tuple[str, str], List[ConnectResult]]]:
    by_id: Dict[str, ConnectResult] = {}
    by_endpoint: Dict[Tuple[str, str], List[ConnectResult]] = {}
    for row in text_results:
        if row.check_id:
            by_id[row.check_id] = row
        ep_a = row.endpoint_a.spec
        ep_b = row.endpoint_b.spec
        if ep_a and ep_b:
            by_endpoint.setdefault((ep_a, ep_b), []).append(row)
    return by_id, by_endpoint


def _representative_text_row(
    rows: Sequence[ConnectResult],
) -> Optional[ConnectResult]:
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    if any(not _leaf_passed_text_conn(r) for r in rows):
        return next(r for r in rows if not _leaf_passed_text_conn(r))
    return rows[0]


def _lookup_text_row_for_leaf(
    leaf: ConnectResult,
    *,
    by_id: Mapping[str, ConnectResult],
    by_endpoint: Mapping[Tuple[str, str], Sequence[ConnectResult]],
) -> Optional[ConnectResult]:
    if leaf.check_id:
        hit = by_id.get(leaf.check_id)
        if hit is not None:
            return hit
    rows = list(by_endpoint.get(
        (leaf.endpoint_a.spec, leaf.endpoint_b.spec),
        (),
    ))
    return _representative_text_row(rows)


def apply_text_verdicts_to_results(
    results: Sequence[ConnectResult],
    text_results: Sequence[ConnectResult],
) -> None:
    """Copy ``connected_text`` (and pre-logical ``connected``) from a prior text pass."""
    by_id, by_endpoint = _build_text_connect_lookup(text_results)
    if not by_id and not by_endpoint:
        return
    for result in flatten_connect_results(results):
        hit = _lookup_text_row_for_leaf(
            result,
            by_id=by_id,
            by_endpoint=by_endpoint,
        )
        if hit is None:
            continue
        text_ok = _leaf_passed_text_conn(hit)
        result.connected_text = text_ok
        result.connected = text_ok


def apply_logical_coi_failure_to_results(
    results: Sequence[ConnectResult],
    logical_checks: Sequence[ConnectivityCheck],
    coi_error: str,
) -> None:
    """Mark scheduled logical checks failed without altering ``connected_text``."""
    if not coi_error or not logical_checks:
        return
    scheduled_ids = {c.check_id for c in logical_checks if c.check_id}
    scheduled_endpoints = {
        (c.endpoint_a, c.endpoint_b) for c in logical_checks
    }
    for leaf in flatten_text_conn_results(results):
        matched = leaf.check_id in scheduled_ids or (
            leaf.endpoint_a.spec,
            leaf.endpoint_b.spec,
        ) in scheduled_endpoints
        if not matched:
            continue
        text_flag = leaf.connected_text
        leaf.connected = False
        leaf.connected_logical = False
        if coi_error not in leaf.errors:
            leaf.errors = [*leaf.errors, coi_error]
        if text_flag is not None:
            leaf.connected_text = text_flag


def _parse_connect_tsv_bool(raw: str) -> bool:
    return str(raw).strip().lower() in ("true", "1", "yes")


def load_text_connect_results_from_tsv(path: Path) -> List[ConnectResult]:
    """Load leaf text-phase rows from ``conn.text.tsv`` for logical gating."""
    src = path.expanduser()
    if not src.is_file():
        return []
    lines = [
        ln
        for ln in src.read_text(encoding="utf-8").splitlines()
        if ln and not ln.startswith("#")
    ]
    if len(lines) < 2:
        return []
    headers = lines[0].split("\t")
    out: List[ConnectResult] = []
    for row_line in lines[1:]:
        cols = row_line.split("\t")
        if len(cols) < len(headers):
            cols.extend([""] * (len(headers) - len(cols)))
        row = dict(zip(headers, cols))
        phase = row.get("phase", "").strip().lower()
        if phase and phase != "text":
            continue
        check_id = row.get("check_id", "").strip()
        ep_a = row.get("endpoint_a", "").strip()
        ep_b = row.get("endpoint_b", "").strip()
        if not ep_a or not ep_b:
            continue
        text_ok = _parse_connect_tsv_bool(
            row.get("connected_text", row.get("connected", "False"))
        )
        out.append(
            ConnectResult(
                ConnectEndpoint(ep_a, "", "", "", port_found=True),
                ConnectEndpoint(ep_b, "", "", "", port_found=True),
                connected=text_ok,
                mode=row.get("mode", "unknown") or "unknown",
                note=row.get("note", ""),
                errors=[
                    e
                    for e in row.get("errors", "").split(" | ")
                    if e.strip()
                ],
                connected_text=text_ok,
                check_id=check_id,
            )
        )
    return out


def build_logical_connect_request(
    request: ConnectivityRequest,
    text_results: Sequence[ConnectResult] | None,
) -> Tuple[ConnectivityRequest, int, int]:
    """
    Build a logical-phase request containing only text-pass leaf checks.

    Returns ``(logical_request, run_count, skipped_count)``.  When
    *text_results* is empty or None (logical-only run), returns *request* unchanged.
    """
    if not text_results:
        return request, len(request.checks), 0

    from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest

    by_id, by_endpoint = _build_text_connect_lookup(text_results)
    logical_checks: List[ConnectivityCheck] = []
    run_count = 0
    skipped_count = 0

    for chk in request.checks:
        hit = by_id.get(chk.check_id)
        if hit is None:
            hit = _representative_text_row(
                by_endpoint.get((chk.endpoint_a, chk.endpoint_b), ())
            )
        if hit is None and chk.check_id:
            prefix = f"{chk.check_id}->"
            expand_leaves = [
                r
                for r in text_results
                if r.check_id and r.check_id.startswith(prefix)
            ]
            if expand_leaves:
                for leaf in expand_leaves:
                    if not _leaf_passed_text_conn(leaf):
                        skipped_count += 1
                        continue
                    logical_checks.append(
                        ConnectivityCheck(
                            leaf.endpoint_a.spec,
                            leaf.endpoint_b.spec,
                            check_id=leaf.check_id,
                        )
                    )
                    run_count += 1
                continue
        if hit is None:
            skipped_count += 1
            continue
        leaves = flatten_text_conn_results([hit])
        if not leaves:
            skipped_count += 1
            continue
        if len(leaves) == 1 and not hit.sub_results:
            if _leaf_passed_text_conn(leaves[0]):
                logical_checks.append(chk)
                run_count += 1
            else:
                skipped_count += 1
            continue
        for leaf in leaves:
            if not _leaf_passed_text_conn(leaf):
                skipped_count += 1
                continue
            logical_checks.append(
                ConnectivityCheck(
                    leaf.endpoint_a.spec,
                    leaf.endpoint_b.spec,
                    check_id=leaf.check_id or chk.check_id,
                )
            )
            run_count += 1

    logical_request = ConnectivityRequest(
        checks=tuple(logical_checks),
        top=request.top,
        defines=dict(request.defines),
        trace=request.trace,
        connect_log=request.connect_log,
        include_ff=request.include_ff,
        strict_generate=request.strict_generate,
        over_approximate_if=request.over_approximate_if,
    )
    return logical_request, run_count, skipped_count


def _merge_one_connect_result(orig: ConnectResult, ref: ConnectResult) -> None:
    """Copy refined structural COI into *orig*, preserving ``connected_text``."""
    text_flag = orig.connected_text
    orig.connected = ref.connected
    orig.mode = ref.mode
    orig.note = ref.note
    orig.errors = list(ref.errors)
    orig.hops = list(ref.hops)
    orig.walk_notes = list(ref.walk_notes)
    orig.coi_walk = ref.coi_walk
    orig.endpoint_a = ref.endpoint_a
    orig.endpoint_b = ref.endpoint_b
    if text_flag is not None:
        orig.connected_text = text_flag
    if ref.sub_results:
        if orig.sub_results and len(orig.sub_results) == len(ref.sub_results):
            for o_sub, r_sub in zip(orig.sub_results, ref.sub_results):
                _merge_one_connect_result(o_sub, r_sub)
        else:
            orig.sub_results = ref.sub_results


def _merge_refined_into_result_tree(
    orig: ConnectResult,
    ref_by_id: Mapping[str, ConnectResult],
) -> None:
    if orig.check_id and orig.check_id in ref_by_id:
        _merge_one_connect_result(orig, ref_by_id[orig.check_id])
    for sub in orig.sub_results or ():
        _merge_refined_into_result_tree(sub, ref_by_id)


def merge_refined_connect_results(
    results: Sequence[ConnectResult],
    refined: Sequence[ConnectResult],
) -> None:
    """Copy post-recovery structural COI into *results*, keeping ``connected_text``.

    Text-conn may reorder checks for cache reuse; merge by ``check_id``, not index.
    Leaf logical results (e.g. expand sub-check ids) merge into parent sub_results.
    """
    if not refined:
        return
    ref_by_id = {r.check_id: r for r in refined if r.check_id}
    if ref_by_id:
        for orig in results:
            _merge_refined_into_result_tree(orig, ref_by_id)
        return
    ref_by_endpoint: Dict[Tuple[str, str], ConnectResult] = {}
    for row in refined:
        ep_a = row.endpoint_a.spec
        ep_b = row.endpoint_b.spec
        if ep_a and ep_b:
            ref_by_endpoint[(ep_a, ep_b)] = row
    if ref_by_endpoint:
        for leaf in flatten_connect_results(results):
            ref = ref_by_endpoint.get(
                (leaf.endpoint_a.spec, leaf.endpoint_b.spec)
            )
            if ref is not None:
                _merge_one_connect_result(leaf, ref)
        return
    raise ValueError(
        "connect refined results lack check_id and endpoint keys; cannot merge"
    )


def reorder_connect_results_to_checks(
    checks: Sequence[ConnectivityCheck],
    results: Sequence[ConnectResult],
) -> List[ConnectResult]:
    """Restore batch result order to match the connect JSON request."""
    by_id = {r.check_id: r for r in results if r.check_id}
    ordered: List[ConnectResult] = []
    seen: set[str] = set()
    for chk in checks:
        hit = by_id.get(chk.check_id)
        if hit is None:
            continue
        ordered.append(hit)
        seen.add(chk.check_id)
    for result in results:
        if result.check_id and result.check_id not in seen:
            ordered.append(result)
        elif not result.check_id:
            ordered.append(result)
    return ordered


def apply_connect_logical_phase(
    results: Sequence[ConnectResult],
    rows_by_path: Mapping[str, FlatRow],
    *,
    run_activation: bool = True,
) -> None:
    """
    Precise logical connectivity on refined structural COI.

    ``connected`` must already reflect post-recovery re-COI.  Logical judgment
    uses that refined structural result — not ``connected_text`` — then applies
    ifdef / provisional / activation downgrades.
    """
    for result in flatten_text_conn_results(results):
        if result.connected_text is None:
            result.connected_text = result.connected

        structural = result.connected
        if not run_activation:
            result.connected_logical = structural
            result.connected = structural
            result.logical_notes = []
            continue

        notes = _walk_notes_for_result(result, rows_by_path)
        logical = structural
        if logical and _endpoints_have_inactive_ifdef(result, rows_by_path):
            logical = False
            notes.append("logical disconnect: inactive ifdef on hierarchy path")
        if logical and _endpoints_have_provisional(result, rows_by_path):
            logical = False
            notes.append("logical disconnect: provisional hierarchy path")
        result.logical_notes = notes
        result.connected_logical = logical
        result.connected = logical
        if notes:
            extra = "; ".join(dict.fromkeys(notes))
            if result.note:
                if extra not in result.note:
                    result.note = f"{result.note}; {extra}"
            else:
                result.note = (
                    f"connect-coi structural only; ifdef-dependent nodes: {extra}"
                    if structural and not logical
                    else extra
                )


def _endpoint_inst_spine(ep: ConnectEndpoint) -> List[str]:
    """Instance spine paths; expands ``[a, b]`` display specs into real paths."""
    specs = hierarchy_endpoint_specs(
        ep.spec,
        inst_path=ep.inst_path,
        port_name=ep.port_name,
        port_found=ep.port_found,
    )
    out: List[str] = []
    seen: set[str] = set()
    for spec_path in specs:
        for path in path_spine_prefixes(spec_path):
            if path in seen:
                continue
            seen.add(path)
            out.append(path)
    return out


def _resolve_endpoint_for_spec(
    spec_path: str,
    rows_by_path: Mapping[str, FlatRow],
    *,
    index: Optional[DesignIndex],
    top: str,
    rows: Sequence[FlatRow],
) -> ConnectEndpoint:
    from hierwalk.connect.shared.endpoints import resolve_endpoint

    ep, _errors = resolve_endpoint(
        spec_path,
        rows,
        index,
        top=top,
        require_port=False,
        rows_by_path=rows_by_path,
    )
    return ep


def normalize_hierarchy_kind(kind: str) -> str:
    """Map probe/trace labels to inst|port|wire|reg."""
    raw = str(kind or "").strip().lower()
    if raw.startswith("port"):
        return "port"
    if raw.startswith("reg"):
        return "reg"
    if raw.startswith("wire"):
        return "wire"
    if raw in HIERARCHY_KINDS:
        return raw
    if raw in ("signal", "not-signal"):
        return "wire"
    return "wire"


def _endpoint_signal_kind(
    ep: ConnectEndpoint,
    rows_by_path: Mapping[str, FlatRow],
    *,
    index: Optional[DesignIndex] = None,
    top: str = "",
) -> str:
    from hierwalk.connect.shared.endpoints import classify_signal_tail_kind

    tail = (ep.port_name or "").strip()
    if not tail and ep.spec and "." in ep.spec:
        tail = ep.spec.rsplit(".", 1)[-1]
    parent = (ep.inst_path or "").strip()
    if not parent and ep.spec and "." in ep.spec:
        parent = ep.spec.rsplit(".", 1)[0]
    row = rows_by_path.get(parent) if parent else None
    if row is not None and index is not None and tail:
        kind = classify_signal_tail_kind(index, row, tail, top=top or parent.split(".", 1)[0])
        if kind in HIERARCHY_KINDS:
            return kind
    if ep.port_found:
        return "port"
    return "wire"


def _endpoint_match_strength(ep: ConnectEndpoint, target_path: str) -> int:
    """Return match strength for *target_path* on *ep* (0 = no match, higher = tighter)."""
    text = (target_path or "").strip()
    if not text:
        return 0
    spec_text = (ep.spec or "").strip()
    best = 0
    for spec_path in hierarchy_endpoint_specs(
        ep.spec,
        inst_path=ep.inst_path,
        port_name=ep.port_name,
        port_found=ep.port_found,
    ):
        if text == spec_path:
            return 100
        if text.startswith(spec_path + "."):
            best = max(best, 80)
        elif spec_path.startswith(text + "."):
            best = max(best, 60)
    parent = (ep.inst_path or "").strip()
    if parent and spec_text == parent:
        if text == parent:
            best = max(best, 50)
        elif text.startswith(parent + "."):
            best = max(best, 40)
    return best


def _endpoint_matches_signal_path(ep: ConnectEndpoint, target_path: str) -> bool:
    """True when *target_path* belongs to *ep* (including ``[a, b]`` display specs)."""
    return _endpoint_match_strength(ep, target_path) > 0


def _match_signal_tail_to_check(
    target_path: str,
    result: ConnectResult,
) -> Optional[tuple[str, int]]:
    """Return ``(side, strength)`` when *target_path* belongs to this check's endpoint."""
    best_side: Optional[str] = None
    best_strength = 0
    for side, ep in (("a", result.endpoint_a), ("b", result.endpoint_b)):
        strength = _endpoint_match_strength(ep, target_path)
        if strength > best_strength:
            best_strength = strength
            best_side = side
    if best_side is None or best_strength <= 0:
        return None
    return best_side, best_strength


def _provenance_for_evidence_path(
    path: str,
    rows_by_path: Mapping[str, FlatRow],
) -> tuple[str, str, str]:
    from hierwalk.hierarchy_log import provenance_fields

    text = (path or "").strip()
    if not text:
        return "", "", ""
    parts = text.split(".")
    for depth in range(len(parts), 0, -1):
        prefix = ".".join(parts[:depth])
        if prefix not in rows_by_path:
            continue
        prov = provenance_fields(prefix, rows_by_path)
        return (
            prov.get("rtl", ""),
            prov.get("via_filelist", ""),
            prov.get("filelist_chain", ""),
        )
    return "", "", ""


def _hierarchy_tsv_headers() -> List[str]:
    return [
        "check_id",
        "side",
        "kind",
        "path",
        "status",
        "module",
        "rtl",
        "via_filelist",
        "filelist_chain",
        "phase",
    ]


def _preferred_hierarchy_paths_for_check(
    chk: ConnectivityCheck,
) -> Dict[tuple[str, str], frozenset[str]]:
    loop_map = dict(chk.expand.loop) if chk.expand and chk.expand.loop else {}
    out: Dict[tuple[str, str], set[str]] = {}
    for pair in expand_check_to_pairs(
        chk.endpoint_a,
        chk.endpoint_b,
        check_id=chk.check_id,
        expand=chk.expand,
    ):
        sub_id = (
            f"{chk.check_id}{pair.sub_id}"
            if chk.check_id and pair.sub_id
            else (chk.check_id or pair.sub_id.strip("[]->"))
        )
        for side, raw in (("a", pair.endpoint_a), ("b", pair.endpoint_b)):
            text = str(raw).strip()
            if not text:
                continue
            if _LOOP_PLACEHOLDER_RE.search(text) and loop_map:
                texts = _expand_looped_endpoint(text, loop_map)
            else:
                texts = (text,)
            paths: set[str] = set()
            for item in texts:
                for spec_path in hierarchy_endpoint_specs(item):
                    if spec_path:
                        paths.add(spec_path)
                        for prefix in path_spine_prefixes(spec_path):
                            paths.add(prefix)
            if paths:
                out.setdefault((sub_id, side), set()).update(paths)
    return {key: frozenset(values) for key, values in out.items()}


def _check_id_keys(chk: ConnectivityCheck) -> frozenset[str]:
    pairs = expand_check_to_pairs(
        chk.endpoint_a,
        chk.endpoint_b,
        check_id=chk.check_id,
        expand=chk.expand,
    )
    keys: set[str] = set()
    for pair in pairs:
        sub = pair.sub_id.strip("[]->")
        if chk.check_id:
            keys.add(f"{chk.check_id}{pair.sub_id}" if pair.sub_id else chk.check_id)
        elif sub:
            keys.add(sub)
    if chk.check_id:
        keys.add(chk.check_id)
    return frozenset(keys)


def collect_hierarchy_evidence_for_check(
    chk: ConnectivityCheck,
    rows_by_path: Mapping[str, FlatRow],
    *,
    rows: Sequence[FlatRow],
    signal_tails: Sequence[SignalTailRecord] = (),
    index: Optional[DesignIndex] = None,
    top: str = "",
) -> List[HierarchyEvidenceRow]:
    """Hierarchy hit/miss rows for one check right after path-walk (no COI yet)."""
    from hierwalk.connect.shared.expand import _placeholder_endpoint

    pairs = expand_check_to_pairs(
        chk.endpoint_a,
        chk.endpoint_b,
        check_id=chk.check_id,
        expand=chk.expand,
    )
    shells: List[ConnectResult] = []
    resolve_cache: Dict[str, ConnectEndpoint] = {}

    def _cached_resolve(spec: str) -> ConnectEndpoint:
        key = str(spec).strip()
        hit = resolve_cache.get(key)
        if hit is not None:
            return hit
        ep = _resolve_endpoint_for_spec(
            key,
            rows_by_path,
            index=index,
            top=top,
            rows=rows,
        )
        resolve_cache[key] = ep
        return ep

    for pair in pairs:
        sub_id = (
            f"{chk.check_id}{pair.sub_id}"
            if chk.check_id and pair.sub_id
            else (chk.check_id or pair.sub_id.strip("[]->"))
        )
        shells.append(
            ConnectResult(
                check_id=sub_id,
                endpoint_a=_cached_resolve(pair.endpoint_a),
                endpoint_b=_cached_resolve(pair.endpoint_b),
                connected=False,
                mode="",
                note="",
            )
        )
    return collect_hierarchy_evidence(
        shells,
        rows_by_path,
        signal_tails=signal_tails,
        index=index,
        top=top,
    )


def _format_hierarchy_evidence_row_line(
    row: HierarchyEvidenceRow,
    *,
    phase: str = "text",
) -> str:
    phase_label = str(phase).strip().lower() or "text"
    return "\t".join(
        (
            row.check_id,
            row.side,
            row.kind,
            row.path,
            row.status,
            row.module,
            row.rtl,
            row.via_filelist,
            row.filelist_chain,
            phase_label,
        )
    )


def format_hierarchy_evidence_tsv(
    evidence: Sequence[HierarchyEvidenceRow],
    *,
    phase: str = "text",
    compact: bool = True,
) -> str:
    phase_label = str(phase).strip().lower() or "text"
    rows = (
        compact_hierarchy_evidence(list(evidence))
        if compact
        else list(evidence)
    )
    lines = ["\t".join(_hierarchy_tsv_headers())]
    for row in rows:
        lines.append(_format_hierarchy_evidence_row_line(row, phase=phase_label))
    return "\n".join(lines) + "\n"


def write_hierarchy_evidence_tsv(
    path: Path,
    evidence: Sequence[HierarchyEvidenceRow],
    *,
    phase: str = "text",
    compact: bool = True,
) -> Path:
    out = path.expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    body = format_hierarchy_evidence_tsv(evidence, phase=phase, compact=compact)
    tmp = out.with_name(f"{out.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(out)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return out


@dataclass
class IncrementalHierarchyTsvWriter:
    """Record hierarchy evidence one row at a time; append rows incrementally."""

    path: Path
    phase: str = "text"
    _rows: List[HierarchyEvidenceRow] = field(default_factory=list)
    _seen: set[tuple[str, str, str, str, str]] = field(default_factory=set)
    _header_written: bool = False

    def _append_row_line(self, row: HierarchyEvidenceRow) -> Path:
        out = self.path.expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        line = _format_hierarchy_evidence_row_line(row, phase=self.phase) + "\n"
        with out.open("a", encoding="utf-8") as fh:
            if not self._header_written:
                if not out.is_file() or out.stat().st_size == 0:
                    fh.write("\t".join(_hierarchy_tsv_headers()) + "\n")
                self._header_written = True
            fh.write(line)
        return out

    def _rewrite_file(self) -> Path:
        out = self.path.expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = ["\t".join(_hierarchy_tsv_headers())]
        for row in self._rows:
            lines.append(_format_hierarchy_evidence_row_line(row, phase=self.phase))
        body = "\n".join(lines) + "\n"
        tmp = out.with_name(f"{out.name}.tmp.{os.getpid()}")
        try:
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(out)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        self._header_written = True
        return out

    def append_row(self, row: HierarchyEvidenceRow) -> Path:
        key = (row.check_id, row.side, row.kind, row.path, row.status)
        if key in self._seen:
            return self.path.expanduser().resolve()
        self._seen.add(key)
        self._rows.append(row)
        return self._append_row_line(row)

    def record_check(
        self,
        chk: ConnectivityCheck,
        rows_by_path: Mapping[str, FlatRow],
        *,
        rows: Sequence[FlatRow],
        signal_tails: Sequence[SignalTailRecord] = (),
        index: Optional[DesignIndex] = None,
        top: str = "",
    ) -> Path:
        keys = _check_id_keys(chk)
        preferred = _preferred_hierarchy_paths_for_check(chk)
        evidence = compact_hierarchy_evidence(
            collect_hierarchy_evidence_for_check(
                chk,
                rows_by_path,
                rows=rows,
                signal_tails=signal_tails,
                index=index,
                top=top,
            ),
            preferred=preferred,
        )
        if keys:
            self._rows = [row for row in self._rows if row.check_id not in keys]
            self._seen = {
                key
                for key in self._seen
                if key[0] not in keys
            }
        for row in evidence:
            key = (row.check_id, row.side, row.kind, row.path, row.status)
            if key in self._seen:
                continue
            self._seen.add(key)
            self._rows.append(row)
        return self._rewrite_file()

    def flush_empty(self) -> Path:
        """Write header-only TSV so downstream tools can watch the file early."""
        return write_hierarchy_evidence_tsv(self.path, (), phase=self.phase)


def collect_hierarchy_evidence(
    results: Sequence[ConnectResult],
    rows_by_path: Mapping[str, FlatRow],
    *,
    signal_tails: Sequence[SignalTailRecord] = (),
    index: Optional[DesignIndex] = None,
    top: str = "",
) -> List[HierarchyEvidenceRow]:
    """Gather inst/port/wire/reg rows for every check (hits and misses)."""
    out: List[HierarchyEvidenceRow] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    flat = flatten_connect_results_for_output(results)

    def _add(
        check_id: str,
        side: str,
        kind: str,
        path: str,
        status: str,
        module: str = "",
    ) -> None:
        norm_kind = normalize_hierarchy_kind(kind)
        if norm_kind not in HIERARCHY_KINDS:
            return
        key = (check_id, side, norm_kind, path, status)
        if key in seen:
            return
        seen.add(key)
        rtl, via_fl, fl_chain = _provenance_for_evidence_path(path, rows_by_path)
        out.append(
            HierarchyEvidenceRow(
                check_id=check_id,
                side=side,
                kind=norm_kind,
                path=path,
                status=status,
                module=module,
                rtl=rtl,
                via_filelist=via_fl,
                filelist_chain=fl_chain,
            )
        )

    for result in flat:
        check_id = result.check_id or ""
        for side, ep in (("a", result.endpoint_a), ("b", result.endpoint_b)):
            for path in _endpoint_inst_spine(ep):
                row = rows_by_path.get(path)
                _add(
                    check_id,
                    side,
                    "inst",
                    path,
                    "hit" if row is not None else "miss",
                    row.module if row is not None else ep.module,
                )
            spec_paths = hierarchy_endpoint_specs(
                ep.spec,
                inst_path=ep.inst_path,
                port_name=ep.port_name,
                port_found=ep.port_found,
            )
            if not spec_paths:
                tail = (ep.port_name or "").strip()
                fallback = (
                    f"{ep.inst_path}.{tail}" if ep.inst_path and tail else tail
                )
                if fallback:
                    spec_paths = (fallback,)
            for spec_path in spec_paths:
                signal_ep = ep
                if spec_path != (ep.spec or "").strip() and index is not None:
                    signal_ep = _resolve_endpoint_for_spec(
                        spec_path,
                        rows_by_path,
                        index=index,
                        top=top,
                        rows=list(rows_by_path.values()),
                    )
                signal_path = (signal_ep.spec or spec_path).strip()
                if not signal_path:
                    continue
                _add(
                    check_id,
                    side,
                    _endpoint_signal_kind(
                        signal_ep,
                        rows_by_path,
                        index=index,
                        top=top,
                    ),
                    signal_path,
                    "hit" if signal_ep.port_found else "miss",
                    signal_ep.module or ep.module,
                )

    for rec in signal_tails:
        if not rec.target_path:
            continue
        kind = normalize_hierarchy_kind(rec.kind)
        status = "hit" if rec.hit else "miss"
        matched_check = ""
        matched_side = ""
        best_strength = 0
        for result in flat:
            hit = _match_signal_tail_to_check(rec.target_path, result)
            if hit is None:
                continue
            side, strength = hit
            if strength > best_strength:
                best_strength = strength
                matched_check = result.check_id or ""
                matched_side = side
        if not matched_check or not matched_side:
            continue
        _add(matched_check, matched_side, kind, rec.target_path, status, rec.module)

    return out


def _hierarchy_compact_row_rank(
    row: HierarchyEvidenceRow,
    *,
    preferred: frozenset[str],
) -> tuple[int, int, int, int, str]:
    """Prefer suite endpoint paths, then hits, then deepest signal rows."""
    depth = len([part for part in row.path.split(".") if part])
    signal_rank = 0 if row.kind == "inst" else 1
    if preferred and row.path in preferred:
        hit_rank = 1 if row.status == "hit" else 0
        inst_bonus = 1 if row.kind == "inst" else 0
        return (3, hit_rank, inst_bonus, -depth, row.path)
    return (0, depth, signal_rank, row.path)


def compact_hierarchy_evidence(
    evidence: Sequence[HierarchyEvidenceRow],
    *,
    preferred: Optional[Mapping[tuple[str, str], frozenset[str]]] = None,
) -> List[HierarchyEvidenceRow]:
    """
    One row per ``(check_id, side)``: the endpoint's final hit/miss only.

    Intermediate inst spine prefixes (``top``, ``top.a``, …) are dropped when a
    deeper port/wire/reg path exists for the same side.  When the endpoint is
    inst-only, the deepest inst path is kept.  Rows matching suite endpoint
    paths in *preferred* win over deeper walked paths.
    """
    pref_map = preferred or {}
    by_group: dict[tuple[str, str], List[HierarchyEvidenceRow]] = {}
    for row in evidence:
        side = row.side if row.side in ("a", "b", "-", "?") else "-"
        by_group.setdefault((row.check_id, side), []).append(row)

    compact: List[HierarchyEvidenceRow] = []
    for key in sorted(by_group, key=lambda item: (item[0], item[1])):
        rows = by_group[key]
        if not rows:
            continue
        pref = pref_map.get(key, frozenset())
        if pref:
            by_path: dict[str, List[HierarchyEvidenceRow]] = {}
            for row in rows:
                if row.path not in pref:
                    continue
                by_path.setdefault(row.path, []).append(row)
            if by_path:
                for path in sorted(by_path):
                    compact.append(
                        max(
                            by_path[path],
                            key=lambda row: _hierarchy_compact_row_rank(
                                row,
                                preferred=pref,
                            ),
                        )
                    )
                continue
        compact.append(
            max(
                rows,
                key=lambda row: _hierarchy_compact_row_rank(
                    row,
                    preferred=pref,
                ),
            )
        )
    return compact


def format_hierarchy_evidence_report(
    evidence: Sequence[HierarchyEvidenceRow],
    *,
    indent: str = "    ",
) -> List[str]:
    """Human-readable inst/port/wire/reg lines grouped by check."""
    if not evidence:
        return ["  (no hierarchy evidence)"]
    by_check: dict[str, List[HierarchyEvidenceRow]] = {}
    for row in evidence:
        by_check.setdefault(row.check_id or "-", []).append(row)
    lines: List[str] = []
    for check_id in sorted(by_check, key=lambda k: (k == "-", k)):
        label = f"[{check_id}]" if check_id and check_id != "-" else "[—]"
        lines.append(f"  {label}")
        for row in by_check[check_id]:
            side = row.side if row.side in ("a", "b") else "·"
            mod = f" ({row.module})" if row.module else ""
            lines.append(
                f"{indent}{side} {row.kind:4} {row.path:40} {row.status}{mod}"
            )
    return lines


def format_connect_results_report(
    results: Sequence[ConnectResult],
    *,
    phase: str = "logical",
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
    signal_tails: Sequence[SignalTailRecord] = (),
    index: Optional[DesignIndex] = None,
    top: str = "",
) -> List[str]:
    """
    Hierarchy-first connect analysis: inst/port/wire/reg evidence, then COI verdict.
    """
    from hierwalk.connect.session import (
        _connected_logical_value,
        _connected_text_value,
    )

    leaf_results = flatten_connect_results_for_output(results)
    if not leaf_results:
        return ["  (no checks)"]
    phase_label = str(phase).strip().lower() or "logical"
    lines: List[str] = []
    lookup = rows_by_path or {}
    evidence = (
        compact_hierarchy_evidence(
            collect_hierarchy_evidence(
                results,
                lookup,
                signal_tails=signal_tails,
                index=index,
                top=top,
            )
        )
        if lookup or signal_tails
        else []
    )
    evidence_by_check: dict[str, List[HierarchyEvidenceRow]] = {}
    for row in evidence:
        evidence_by_check.setdefault(row.check_id or "", []).append(row)

    for result in leaf_results:
        cid = result.check_id or ""
        pair = f"{result.endpoint_a.spec} -> {result.endpoint_b.spec}"
        header = f"  [{cid}] {pair}" if cid else f"  {pair}"
        lines.append(header)
        check_evidence = evidence_by_check.get(cid, [])
        if check_evidence:
            for ev in check_evidence:
                mod = f" ({ev.module})" if ev.module else ""
                lines.append(
                    f"    {ev.side} {ev.kind:4} {ev.path:40} {ev.status}{mod}"
                )
        else:
            for side, ep in (("a", result.endpoint_a), ("b", result.endpoint_b)):
                if ep.spec:
                    lines.append(f"    {side} ---- {ep.spec:40} (unresolved)")
        text_ok = _connected_text_value(result)
        logical_ok = _connected_logical_value(result)
        if phase_label == "text":
            coi = "PASS" if text_ok else "FAIL"
            lines.append(f"    coi(text): {coi}")
        elif phase_label == "logical":
            coi = "PASS" if logical_ok else "FAIL"
            lines.append(f"    coi(logical): {coi}")
        else:
            lines.append(
                f"    coi: text={'PASS' if text_ok else 'FAIL'} "
                f"logical={'PASS' if logical_ok else 'FAIL'}"
            )
        if result.errors:
            err = " | ".join(result.errors)
            lines.append(f"    note: {err}")
        elif result.note:
            lines.append(f"    note: {result.note}")
    return lines


def format_connect_hierarchy_tsv(
    results: Sequence[ConnectResult],
    rows_by_path: Mapping[str, FlatRow],
    *,
    phase: str = "text",
    signal_tails: Sequence[SignalTailRecord] = (),
    index: Optional[DesignIndex] = None,
    top: str = "",
    compact: bool = True,
) -> str:
    evidence = collect_hierarchy_evidence(
        results,
        rows_by_path,
        signal_tails=signal_tails,
        index=index,
        top=top,
    )
    return format_hierarchy_evidence_tsv(
        evidence,
        phase=phase,
        compact=compact,
    )


def write_connect_phase_tsv(
    path: Path,
    results: Sequence[ConnectResult],
    *,
    phase: str,
    modules_cached: Optional[int] = None,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
) -> Path:
    body = format_connect_results_tsv(
        results,
        modules_cached=modules_cached,
        rows_by_path=rows_by_path,
        phase=phase,
    )
    out = path.expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(out)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return out


def require_connect_phase_tsv(
    path: Path,
    results: Sequence[ConnectResult],
    *,
    phase: str,
    modules_cached: Optional[int] = None,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
) -> Path:
    """Write connect phase TSV and fail if the file is not on disk afterward."""
    out = write_connect_phase_tsv(
        path,
        results,
        phase=phase,
        modules_cached=modules_cached,
        rows_by_path=rows_by_path,
    )
    if not out.is_file():
        raise OSError(f"connect phase TSV not created: {out}")
    if out.stat().st_size == 0:
        raise OSError(f"connect phase TSV is empty: {out}")
    return out