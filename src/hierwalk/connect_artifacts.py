"""Two-phase connect artifacts (text-conn / logical-conn) under the per-run db folder."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Union

from hierwalk.connect_expand import (
    aggregate_connect_results,
    expand_check_to_pairs,
    hierarchy_endpoint_specs,
)
from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.connectivity import (
    ConnectivitySession,
    flatten_connect_results,
    flatten_connect_results_for_output,
    format_connect_results_tsv,
)
from hierwalk.hierarchy_log import path_spine_prefixes, resolve_absolute_rtl_path
from hierwalk.index import DesignIndex
from hierwalk.models import ConnectEndpoint, ConnectResult, FlatRow
from hierwalk.run_request import RunConfig

HIERARCHY_KINDS = frozenset({"inst", "port", "wire", "reg"})


@dataclass(frozen=True)
class HierarchyEvidenceRow:
    """One resolved hierarchy element for a connect check (inst/port/wire/reg)."""

    check_id: str
    side: str
    kind: str
    path: str
    status: str
    module: str = ""
    rtl_path: str = ""


def _hierarchy_rtl_path(
    path: str,
    kind: str,
    rows_by_path: Mapping[str, FlatRow],
) -> str:
    """Absolute RTL file for an inst scope or the parent module of a signal tail."""
    row = rows_by_path.get(path)
    if row is not None and row.file:
        return resolve_absolute_rtl_path(row.file)
    if "." in path:
        parent = path.rsplit(".", 1)[0]
        parent_row = rows_by_path.get(parent)
        if parent_row is not None and parent_row.file:
            return resolve_absolute_rtl_path(parent_row.file)
    return ""


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
    from hierwalk.connect_endpoints import resolve_endpoint

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


def merge_refined_connect_results(
    results: Sequence[ConnectResult],
    refined: Sequence[ConnectResult],
) -> None:
    """Copy post-recovery structural COI into *results*, keeping ``connected_text``."""
    orig_flat = flatten_connect_results(results)
    ref_flat = flatten_connect_results(refined)
    if len(orig_flat) != len(ref_flat):
        raise ValueError(
            f"connect result length mismatch: {len(orig_flat)} vs {len(ref_flat)}"
        )
    for orig, ref in zip(orig_flat, ref_flat):
        orig.connected = ref.connected
        orig.mode = ref.mode
        orig.note = ref.note
        orig.errors = list(ref.errors)
        orig.hops = list(ref.hops)
        orig.walk_notes = list(ref.walk_notes)
        orig.coi_walk = ref.coi_walk
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
    from hierwalk.connect_endpoints import resolve_endpoint

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
    from hierwalk.connect_endpoints import classify_signal_tail_kind

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


def _endpoint_matches_signal_path(ep: ConnectEndpoint, target_path: str) -> bool:
    """True when *target_path* belongs to *ep* (including ``[a, b]`` display specs)."""
    text = (target_path or "").strip()
    if not text:
        return False
    for spec_path in hierarchy_endpoint_specs(
        ep.spec,
        inst_path=ep.inst_path,
        port_name=ep.port_name,
        port_found=ep.port_found,
    ):
        if text == spec_path:
            return True
        if spec_path.endswith("." + text.rsplit(".", 1)[-1]):
            return True
        if text.startswith(spec_path + ".") or spec_path.startswith(text + "."):
            return True
    parent = (ep.inst_path or "").strip()
    if parent and (text == parent or text.startswith(parent + ".")):
        return True
    return False


def _match_signal_tail_to_check(
    target_path: str,
    result: ConnectResult,
) -> Optional[str]:
    """Return ``a`` or ``b`` when *target_path* belongs to this check's endpoint."""
    for side, ep in (("a", result.endpoint_a), ("b", result.endpoint_b)):
        if _endpoint_matches_signal_path(ep, target_path):
            return side
    return None


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
        out.append(
            HierarchyEvidenceRow(
                check_id=check_id,
                side=side,
                kind=norm_kind,
                path=path,
                status=status,
                module=module,
                rtl_path=_hierarchy_rtl_path(path, norm_kind, rows_by_path),
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
        matched_side = "-"
        for result in flat:
            side = _match_signal_tail_to_check(rec.target_path, result)
            if side is not None:
                matched_check = result.check_id or ""
                matched_side = side
                break
        _add(matched_check, matched_side, kind, rec.target_path, status, rec.module)

    return out


def compact_hierarchy_evidence(
    evidence: Sequence[HierarchyEvidenceRow],
) -> List[HierarchyEvidenceRow]:
    """
    Drop redundant inst spine hits; keep miss steps and deepest inst per side.

    Intermediate ``top`` / ``top.a`` hits are omitted when ``top.a.b.c`` is the
    resolved inst path.  Miss prefixes (e.g. ``top.missing``) are always kept.
    Port/wire/reg rows are unchanged.
    """
    inst_by_group: dict[tuple[str, str], List[HierarchyEvidenceRow]] = {}
    other: List[HierarchyEvidenceRow] = []
    for row in evidence:
        if row.kind == "inst":
            inst_by_group.setdefault((row.check_id, row.side), []).append(row)
        else:
            other.append(row)

    compact: List[HierarchyEvidenceRow] = []
    for (_cid, _side), rows in inst_by_group.items():
        misses = [r for r in rows if r.status == "miss"]
        hits = [r for r in rows if r.status == "hit"]
        compact.extend(misses)
        if hits:
            compact.append(max(hits, key=lambda r: (len(r.path), r.path)))
    compact.extend(other)
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
            rtl = f" rtl={row.rtl_path}" if row.rtl_path else ""
            lines.append(
                f"{indent}{side} {row.kind:4} {row.path:40} {row.status}{mod}{rtl}"
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
    from hierwalk.connectivity import (
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
                rtl = f" rtl={ev.rtl_path}" if ev.rtl_path else ""
                lines.append(
                    f"    {ev.side} {ev.kind:4} {ev.path:40} {ev.status}{mod}{rtl}"
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
    phase_label = str(phase).strip().lower() or "text"
    headers = ["check_id", "side", "kind", "path", "status", "module", "rtl_path", "phase"]
    evidence = collect_hierarchy_evidence(
        results,
        rows_by_path,
        signal_tails=signal_tails,
        index=index,
        top=top,
    )
    if compact:
        evidence = compact_hierarchy_evidence(evidence)
    lines = ["\t".join(headers)]
    for row in evidence:
        lines.append(
            "\t".join(
                (
                    row.check_id,
                    row.side,
                    row.kind,
                    row.path,
                    row.status,
                    row.module,
                    row.rtl_path,
                    phase_label,
                )
            )
        )
    return "\n".join(lines) + "\n"


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