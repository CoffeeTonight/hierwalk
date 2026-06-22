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


def connect_output_paths(work_dir: Path) -> ConnectOutputPaths:
    root = work_dir.expanduser().resolve()
    return ConnectOutputPaths(
        text_tsv=root / "conn.text.tsv",
        logical_tsv=root / "conn.tsv",
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