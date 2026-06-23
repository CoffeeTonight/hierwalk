"""Two-phase connect artifacts (text-conn / logical-conn) under the per-run db folder."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Union

from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.connectivity import (
    flatten_connect_results,
    format_connect_results_tsv,
)
from hierwalk.models import ConnectEndpoint, ConnectResult, FlatRow
from hierwalk.run_request import RunConfig


@dataclass(frozen=True)
class ConnectOutputPaths:
    text_tsv: Path
    logical_tsv: Path


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
    return ConnectOutputPaths(
        text_tsv=root / text_name,
        logical_tsv=root / logical_name,
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

    active = get_active_work_dir()
    if active is not None:
        return active
    if cache_dir is not None:
        cache_root = cache_dir.expanduser().resolve()
        if cache_root.name.startswith(".db_"):
            cache_root.mkdir(parents=True, exist_ok=True)
            return cache_root
        resolved_base = cache_root
    else:
        resolved_base = base
    if resolved_base is None:
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
    """Write phase TSV if missing; always overwrites with *results* when invoked."""
    paths = connect_output_paths(work_dir, output)
    target = paths.text_tsv if phase == "text" else paths.logical_tsv
    return write_connect_phase_tsv(
        target,
        results,
        phase=phase,
        modules_cached=modules_cached,
        rows_by_path=rows_by_path,
    )


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


def any_text_conn_hit(results: Sequence[ConnectResult]) -> bool:
    """True when at least one leaf check passed text-conn (worth a logical pass)."""
    return any(r.connected_text for r in flatten_text_conn_results(results))


def merge_refined_connect_results(
    results: Sequence[ConnectResult],
    refined: Sequence[ConnectResult],
) -> None:
    """Copy post-recovery structural COI into *results*, keeping ``connected_text``."""
    for orig, ref in zip(
        flatten_connect_results(results),
        flatten_connect_results(refined),
    ):
        orig.connected = ref.connected
        orig.mode = ref.mode
        orig.note = ref.note
        orig.errors = list(ref.errors)
        orig.hops = list(ref.hops)
        orig.endpoint_a = ref.endpoint_a
        orig.endpoint_b = ref.endpoint_b


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
    out.write_text(body, encoding="utf-8")
    return out