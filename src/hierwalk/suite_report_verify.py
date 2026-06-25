"""Execute flat-suite JSON and validate artifact / log report shape."""

from __future__ import annotations

import csv
import io
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from dataclasses import replace

from hierwalk.cli_execute import execute_run
from hierwalk.connect_artifacts import (
    archive_run_config_sources,
    connect_output_paths,
    hierarchy_output_basename,
    work_dir_artifact_path,
)
from hierwalk.connect_expand import hierarchy_endpoint_specs, parse_list_display_spec
from hierwalk.cache import resolve_run_work_dir, work_base_dir
from hierwalk.run_request import RUN_CONE_TRACE, RUN_CONN_CHECK, RUN_IO_TRACE, RunConfig
from hierwalk.report_provenance import (
    connect_phase_timings,
    report_command_line,
    report_header_lines,
)
from hierwalk.run_tests import (
    RunTestEntry,
    build_test_run_configs,
    expand_suite_verification_plan,
    parse_run_test_suite,
    spec_for_test_entry,
)
from hierwalk.verification_timing import (
    StepTiming,
    VerificationTimingRecorder,
    bind_suite_recorder,
    verification_step,
    verification_step_label,
)


@dataclass
class StepIssue:
    step: str
    kind: str
    message: str


@dataclass
class StepOutcomeStats:
    """Issue count vs total items for percentage display in details."""

    total: int = 0
    issues: int = 0
    label: str = "checks"

    def ratio_line(self) -> str:
        if self.total <= 0:
            return f"0/0 {self.label} (n/a)"
        pct = 100.0 * self.issues / self.total
        return f"{self.issues}/{self.total} {self.label} ({pct:.1f}%)"


@dataclass
class StepErrorLine:
    """One issue row for the per-step Errors block (sorted by symptom)."""

    symptom: str
    subject: str
    tag: str
    detail: str


@dataclass
class StepOutcomeSummary:
    """Per verification step: timing, artifacts, log, and result outcome notes."""

    name: str
    kind: str
    elapsed_sec: float = 0.0
    artifacts: List[Path] = field(default_factory=list)
    log_path: Optional[Path] = None
    errors: List[StepErrorLine] = field(default_factory=list)
    stats: Optional[StepOutcomeStats] = None
    execute_ok: bool = True


@dataclass
class SuiteVerifyReport:
    suite_path: Path
    work_dir: Path
    steps_run: int = 0
    steps_failed: int = 0
    issues: List[StepIssue] = field(default_factory=list)
    started_at: Optional[datetime] = None
    elapsed_sec: float = 0.0
    command: str = ""
    cwd: Optional[Path] = None
    user: str = ""
    timing_steps: List[StepTiming] = field(default_factory=list)
    step_summaries: List[StepOutcomeSummary] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.steps_failed == 0 and not self.issues


def _tsv_rows(text: str) -> List[Dict[str, str]]:
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    if not lines:
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter="\t")
    return list(reader)


def _comment_lines(text: str) -> List[str]:
    return [ln for ln in text.splitlines() if ln.startswith("#")]


def _artifact_path_for_step(
    work_dir: Path,
    cfg: RunConfig,
    *,
    phase: str = "",
) -> Path:
    phase_label = (phase or cfg.verification_phase or "logical").strip().lower()
    if cfg.verification_step_kind == RUN_CONN_CHECK:
        paths = connect_output_paths(work_dir, cfg.output)
        if phase_label == "text":
            return paths.text_tsv
        return paths.logical_tsv
    if phase_label == "text":
        return work_dir_artifact_path(work_dir, cfg.output, phase="text")
    return Path(cfg.output) if cfg.output != "-" else work_dir / Path(cfg.output).name


def _hierarchy_path_for_conn(work_dir: Path, cfg: RunConfig, *, phase: str) -> Path:
    hier_name = hierarchy_output_basename(cfg.output)
    if phase == "text":
        stem = Path(hier_name).stem
        suffix = Path(hier_name).suffix or ".tsv"
        return work_dir / f"{stem}.text{suffix}"
    return work_dir / hier_name


def _expected_conn_outcomes(spec: Mapping[str, Any]) -> Dict[str, bool]:
    out: Dict[str, bool] = {}
    checks = spec.get("checks") or []
    if not isinstance(checks, list):
        return out
    for item in checks:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or item.get("name") or "").strip()
        if not cid:
            continue
        expect = item.get("expect_connected")
        if expect is None:
            expect = item.get("expect")
        if isinstance(expect, dict):
            val = expect.get("connected")
            if val is not None:
                out[cid] = bool(val)
        elif expect is not None:
            out[cid] = bool(expect)
    return out


def _validate_conn_tsv(
    path: Path,
    *,
    phase: str,
    expected: Mapping[str, bool],
    step_label: str,
) -> List[StepIssue]:
    issues: List[StepIssue] = []
    if not path.is_file():
        return [StepIssue(step_label, "artifact", f"missing conn TSV: {path}")]
    rows = _tsv_rows(path.read_text(encoding="utf-8"))
    if not rows:
        issues.append(StepIssue(step_label, "format", "conn TSV has no data rows"))
        return issues
    headers = set(rows[0].keys())
    for col in ("check_id", "endpoint_a", "endpoint_b"):
        if col not in headers:
            issues.append(StepIssue(step_label, "format", f"conn TSV missing column {col!r}"))
    phase_col = "connected_text" if phase == "text" else "connected_logical"
    if phase_col not in headers and "connected" not in headers:
        issues.append(
            StepIssue(step_label, "format", f"conn TSV missing {phase_col!r}")
        )
    for row in rows:
        cid = row.get("check_id", "")
        if not cid:
            continue
        if phase == "text":
            got = (row.get("connected_text") or row.get("connected", "")).strip().lower()
        else:
            got = (row.get("connected_logical") or row.get("connected", "")).strip().lower()
        connected = got in ("1", "true", "yes", "pass")
        for parent, exp in expected.items():
            if not _check_id_matches(parent, cid):
                continue
            if phase == "text" and exp is False:
                # Text-conn is bloom/coarse; negative expectations are logical-only.
                break
            if connected != exp:
                issues.append(
                    StepIssue(
                        step_label,
                        "verdict",
                        f"{cid}: expected connected={exp} got {got!r}",
                    )
                )
            break
    return issues


def _check_id_matches(parent: str, row_cid: str) -> bool:
    if not parent:
        return True
    if row_cid == parent:
        return True
    if row_cid.startswith((f"{parent}:", f"{parent}->", f"{parent}[")):
        return True
    # legacy duplicate prefix from pre-fix _zip_array_pairs
    return row_cid.startswith(f"{parent}{parent}")


def _hierarchy_rows_for_expectation(
    rows: Sequence[Mapping[str, str]],
    *,
    check_id: str,
    side: str,
    path: str,
) -> List[Mapping[str, str]]:
    out: List[Mapping[str, str]] = []
    for row in rows:
        if row.get("path") != path:
            continue
        row_side = row.get("side", "")
        if side and row_side not in (side, "-", "?"):
            continue
        if check_id and not _check_id_matches(check_id, str(row.get("check_id", ""))):
            continue
        out.append(row)
    return out


def _validate_expect_hierarchy(
    rows: Sequence[Mapping[str, str]],
    checks: Sequence[Mapping[str, Any]],
    *,
    step_label: str,
) -> List[StepIssue]:
    issues: List[StepIssue] = []
    for item in checks:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or item.get("name") or "").strip()
        expect_hier = item.get("expect_hierarchy")
        if not isinstance(expect_hier, list):
            continue
        for exp in expect_hier:
            if not isinstance(exp, dict):
                continue
            path = str(exp.get("path") or "").strip()
            if not path:
                continue
            side = str(exp.get("side") or "a").strip()
            module = str(exp.get("module") or "").strip()
            rtl_file = str(exp.get("rtl_file") or exp.get("rtl") or "").strip()
            kind = str(exp.get("kind") or "inst").strip()
            status = str(exp.get("status") or "hit").strip()
            matches = _hierarchy_rows_for_expectation(
                rows,
                check_id=cid,
                side=side,
                path=path,
            )
            if kind:
                matches = [r for r in matches if r.get("kind") == kind] or matches
            if status:
                matches = [r for r in matches if r.get("status") == status] or matches
            if not matches:
                issues.append(
                    StepIssue(
                        step_label,
                        "multi_module",
                        f"{cid}: no hierarchy row for {path!r} (side={side})",
                    )
                )
                continue
            row = matches[0]
            if module and row.get("module") != module:
                issues.append(
                    StepIssue(
                        step_label,
                        "multi_module",
                        f"{cid}: {path!r} module expected {module!r} got {row.get('module')!r}",
                    )
                )
            rtl = str(row.get("rtl") or "")
            if rtl_file and rtl_file not in rtl:
                issues.append(
                    StepIssue(
                        step_label,
                        "multi_module",
                        f"{cid}: {path!r} rtl expected *{rtl_file!r}* got {rtl!r}",
                    )
                )
    return issues


def _hierarchy_covers_path(
    rows: Sequence[Mapping[str, str]],
    *,
    path: str,
    side: str,
    check_id: str,
) -> bool:
    for row in rows:
        if row.get("path") != path:
            continue
        row_side = row.get("side", "")
        row_cid = row.get("check_id", "")
        if row_side not in (side, "-", "?"):
            continue
        if _check_id_matches(check_id, row_cid):
            return True
        if row.get("kind") == "inst" and row.get("status") == "hit":
            return True
    return False


def _validate_hierarchy_tsv(
    path: Path,
    *,
    spec: Mapping[str, Any],
    phase: str,
    step_label: str,
) -> List[StepIssue]:
    issues: List[StepIssue] = []
    if not path.is_file():
        return [StepIssue(step_label, "artifact", f"missing hierarchy TSV: {path}")]
    rows = _tsv_rows(path.read_text(encoding="utf-8"))
    if not rows:
        issues.append(StepIssue(step_label, "format", "hierarchy TSV has no data rows"))
        return issues
    headers = set(rows[0].keys())
    for col in ("check_id", "side", "kind", "path", "status", "rtl", "phase"):
        if col not in headers:
            issues.append(
                StepIssue(step_label, "format", f"hierarchy TSV missing column {col!r}")
            )
    for row in rows:
        if "[zz" in row.get("path", "") or row.get("path", "").startswith("["):
            issues.append(
                StepIssue(
                    step_label,
                    "bracket",
                    f"raw bracket path in hierarchy: {row.get('path')!r}",
                )
            )
        if row.get("status") == "hit" and not row.get("rtl", "").strip():
            issues.append(
                StepIssue(
                    step_label,
                    "rtl",
                    f"hit node missing rtl: {row.get('path')!r}",
                )
            )
    checks = spec.get("checks") or []
    if isinstance(checks, list):
        for item in checks:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or item.get("name") or "").strip()
            for side_key, side in (("a", "a"), ("b", "b")):
                raw = item.get(side_key)
                if raw is None:
                    continue
                if isinstance(raw, (list, tuple)):
                    expected_paths = tuple(str(x).strip() for x in raw)
                else:
                    text = str(raw).strip()
                    listed = parse_list_display_spec(text)
                    expected_paths = listed if listed is not None else (text,)
                for ep in expected_paths:
                    if not ep or ep.startswith("{"):
                        continue
                    specs = hierarchy_endpoint_specs(ep)
                    for spec_path in specs:
                        if _hierarchy_covers_path(
                            rows,
                            path=spec_path,
                            side=side,
                            check_id=cid,
                        ):
                            continue
                        if any(
                            spec_path.startswith(r.get("path", "") + ".")
                            for r in rows
                            if r.get("kind") == "inst"
                            and r.get("status") == "hit"
                            and _check_id_matches(cid, str(r.get("check_id", "")))
                        ):
                            continue
                        issues.append(
                            StepIssue(
                                step_label,
                                "coverage",
                                f"{cid} side {side}: expected path {spec_path!r} in hierarchy",
                            )
                        )
    if phase and rows and any(r.get("phase") != phase for r in rows):
        bad = {r.get("phase") for r in rows if r.get("phase") != phase}
        issues.append(
            StepIssue(step_label, "phase", f"hierarchy phase mismatch: {bad!r}")
        )
    if isinstance(checks, list):
        issues.extend(
            _validate_expect_hierarchy(rows, checks, step_label=step_label)
        )
    return issues


def _validate_cone_tsv(path: Path, *, step_label: str) -> List[StepIssue]:
    issues: List[StepIssue] = []
    if not path.is_file():
        return [StepIssue(step_label, "artifact", f"missing cone TSV: {path}")]
    text = path.read_text(encoding="utf-8")
    rows = _tsv_rows(text)
    comments = _comment_lines(text)
    if not rows:
        issues.append(StepIssue(step_label, "format", "cone TSV has no data rows"))
    else:
        headers = set(rows[0].keys())
        for col in ("kind", "scope", "net", "rtl"):
            if col not in headers:
                issues.append(
                    StepIssue(step_label, "format", f"cone TSV missing column {col!r}")
                )
        if not any(r.get("rtl", "").strip() for r in rows):
            issues.append(StepIssue(step_label, "rtl", "cone TSV has no rtl paths"))
    for tag in ("# origin\t", "# direction\t"):
        if not any(ln.startswith(tag) for ln in comments):
            issues.append(StepIssue(step_label, "format", f"cone TSV missing {tag.strip()}"))
    return issues


def _validate_io_trace_tsv(path: Path, *, step_label: str) -> List[StepIssue]:
    issues: List[StepIssue] = []
    if not path.is_file():
        return [StepIssue(step_label, "artifact", f"missing io_trace TSV: {path}")]
    text = path.read_text(encoding="utf-8")
    rows = _tsv_rows(text)
    comments = _comment_lines(text)
    if not rows:
        if not any(ln.startswith("# errors\t") for ln in comments):
            issues.append(StepIssue(step_label, "format", "io_trace TSV has no data rows"))
    else:
        headers = set(rows[0].keys())
        for col in ("origin_port", "boundary_kind", "scope", "rtl"):
            if col not in headers:
                issues.append(
                    StepIssue(step_label, "format", f"io_trace TSV missing column {col!r}")
                )
    for tag in ("# instance\t", "# direction\t", "# path_kind\t"):
        if not any(ln.startswith(tag) for ln in comments):
            issues.append(StepIssue(step_label, "format", f"io_trace TSV missing {tag.strip()}"))
    if not any(ln.startswith("# instance_rtl\t") for ln in comments):
        issues.append(StepIssue(step_label, "rtl", "io_trace missing # instance_rtl"))
    return issues


def _validate_log(path: Path, *, step_label: str, phase: str = "") -> List[StepIssue]:
    issues: List[StepIssue] = []
    if not path.is_file():
        return [StepIssue(step_label, "log", f"missing log: {path}")]
    text = path.read_text(encoding="utf-8")
    if phase == "text":
        markers = ("connect-text-conn", "# connect results (text)", "Connectivity")
    elif phase == "logical":
        markers = ("connect-logical-conn", "# connect results (logical)", "Connectivity")
    elif "cone" in step_label.lower():
        low = step_label.lower()
        if "fanin" in low:
            markers = ("cone fanin:", "visited nets:")
        elif "fanout" in low:
            markers = ("cone fanout:", "visited nets:")
        else:
            markers = ("visited nets:",)
    elif "io" in step_label.lower():
        markers = ("inst-trace:", "port traces:")
    else:
        markers = ("Connectivity",)
    for marker in markers:
        if marker not in text:
            issues.append(
                StepIssue(step_label, "log", f"log missing marker {marker!r}")
            )
    return issues


def _abs_path(path: Path, *, base_dir: Path) -> Path:
    p = path.expanduser()
    if p.is_absolute():
        return p.resolve()
    return (base_dir / p).resolve()


def _log_path_for_output(output: str, work_dir: Path, *, phase: str = "") -> Path:
    stem = Path(output).stem if output and output != "-" else "output"
    suffix = Path(output).suffix or ".tsv"
    if phase == "text":
        return work_dir / f"{stem}.text.hier-walk.log"
    return work_dir / f"{stem}.hier-walk.log"


def _artifact_paths_for_step(
    entry: RunTestEntry,
    cfg: RunConfig,
    *,
    work_dir: Path,
    base_dir: Path,
) -> Tuple[List[Path], Optional[Path]]:
    """Resolved artifact TSV paths and primary log for a suite step."""
    phase = (cfg.verification_phase or "logical").strip().lower()
    artifacts: List[Path] = []
    log_path: Optional[Path] = None
    if entry.kind == RUN_CONN_CHECK:
        art = _artifact_path_for_step(work_dir, cfg, phase=phase)
        hier = _hierarchy_path_for_conn(work_dir, cfg, phase=phase)
        artifacts.extend([art, hier])
        log_path = _log_path_for_output(cfg.output, work_dir, phase=phase)
    elif entry.kind in (RUN_CONE_TRACE, RUN_IO_TRACE):
        out = _abs_path(Path(cfg.output), base_dir=base_dir)
        artifacts.append(out)
        log_path = _log_path_for_output(cfg.output, work_dir)
    return artifacts, log_path


def _conn_row_connected(row: Mapping[str, str], *, phase: str) -> bool:
    if phase == "text":
        got = (row.get("connected_text") or row.get("connected", "")).strip().lower()
    else:
        got = (row.get("connected_logical") or row.get("connected", "")).strip().lower()
    return got in ("1", "true", "yes", "pass")


def _short_note(text: str, *, limit: int = 120) -> str:
    one = " ".join(text.split())
    if len(one) <= limit:
        return one
    return one[: limit - 3] + "..."


def _short_error_tag(tag: str) -> str:
    mapping = {
        "review (JSON typo / RTL / endpoint?)": "review",
        "expected in logical only": "expected-logical",
        "expected disconnect": "expected",
        "text bloom pass — logical should fail": "bloom-pass",
        "connected with errors": "conn-errors",
        "artifact": "artifact",
        "trace": "trace",
    }
    return mapping.get(tag, tag)


def _symptom_from_detail(detail: str, *, tag: str = "") -> str:
    low = detail.lower()
    if "hierarchy not found" in low:
        return "hierarchy not found"
    if "does not reach hierarchy" in low:
        return "does not reach hierarchy"
    if "no path" in low:
        return "no path"
    if "signal/port not found" in low or "port not found" in low:
        return "signal/port not found"
    if "missing artifact" in low:
        return "missing artifact"
    if "no data rows" in low:
        return "no data rows"
    if "rows missing rtl" in low:
        return "rows missing rtl"
    if tag == "connected with errors":
        return "connected with errors"
    if tag == "text bloom pass — logical should fail":
        return "text bloom pass"
    if tag == "trace":
        return "trace error"
    return "other"


def _format_sorted_error_lines(errors: Sequence[StepErrorLine]) -> List[str]:
    """Errors block only when non-empty (omit entirely for vim-friendly search)."""
    if not errors:
        return []
    ordered = sorted(
        errors,
        key=lambda e: (e.symptom, _short_error_tag(e.tag), e.subject),
    )
    lines = ["  Errors:"]
    for err in ordered:
        tag = _short_error_tag(err.tag)
        lines.append(
            f"    {err.symptom} | {err.subject} | {tag} | {err.detail}"
        )
    return lines


def _summarize_conn_outcomes(
    path: Path,
    *,
    phase: str,
    spec: Mapping[str, Any],
) -> Tuple[StepOutcomeStats, List[StepErrorLine]]:
    """Disconnected checks and errors — may indicate JSON typos or RTL gaps."""
    stats = StepOutcomeStats(label="checks")
    errors: List[StepErrorLine] = []
    if not path.is_file():
        detail = f"missing artifact: {path}"
        stats.issues = 1
        stats.total = 1
        errors.append(
            StepErrorLine(
                symptom=_symptom_from_detail(detail),
                subject="artifact",
                tag="artifact",
                detail=detail,
            )
        )
        return stats, errors
    rows = _tsv_rows(path.read_text(encoding="utf-8"))
    expected = _expected_conn_outcomes(spec)
    expect_false = {k for k, v in expected.items() if v is False}
    for row in rows:
        cid = row.get("check_id", "")
        if not cid:
            continue
        stats.total += 1
        connected = _conn_row_connected(row, phase=phase)
        row_errors = (row.get("errors") or "").strip()
        note = (row.get("note") or "").strip()
        is_expected = any(_check_id_matches(parent, cid) for parent in expect_false)
        if connected and phase == "text" and is_expected:
            stats.issues += 1
            tag = "text bloom pass — logical should fail"
            detail = _short_note(note or row_errors or "connected")
            errors.append(
                StepErrorLine(
                    symptom=_symptom_from_detail(detail, tag=tag),
                    subject=cid,
                    tag=tag,
                    detail=detail,
                )
            )
            continue
        if not connected:
            stats.issues += 1
            if phase == "text" and is_expected:
                tag = "expected in logical only"
            elif is_expected:
                tag = "expected disconnect"
            else:
                tag = "review (JSON typo / RTL / endpoint?)"
            detail = _short_note(row_errors or note or "disconnected")
            errors.append(
                StepErrorLine(
                    symptom=_symptom_from_detail(detail, tag=tag),
                    subject=cid,
                    tag=tag,
                    detail=detail,
                )
            )
        elif row_errors:
            stats.issues += 1
            tag = "connected with errors"
            detail = _short_note(row_errors)
            errors.append(
                StepErrorLine(
                    symptom=_symptom_from_detail(detail, tag=tag),
                    subject=cid,
                    tag=tag,
                    detail=detail,
                )
            )
    return stats, errors


def _summarize_io_cone_outcomes(
    path: Path, *, kind: str
) -> Tuple[StepOutcomeStats, List[StepErrorLine]]:
    stats = StepOutcomeStats(label="rows")
    errors: List[StepErrorLine] = []
    if not path.is_file():
        detail = f"missing artifact: {path}"
        stats.issues = 1
        stats.total = 1
        errors.append(
            StepErrorLine(
                symptom=_symptom_from_detail(detail),
                subject="artifact",
                tag="artifact",
                detail=detail,
            )
        )
        return stats, errors
    text = path.read_text(encoding="utf-8")
    comments = _comment_lines(text)
    rows = _tsv_rows(text)
    header_errors = 0
    for ln in comments:
        if ln.startswith("# errors\t"):
            err = ln.split("\t", 1)[-1].strip()
            if err:
                header_errors += 1
                detail = _short_note(err)
                errors.append(
                    StepErrorLine(
                        symptom="trace error",
                        subject="trace",
                        tag="trace",
                        detail=detail,
                    )
                )
    stats.total = len(rows) if rows else (1 if header_errors else 0)
    if not rows:
        stats.issues = max(stats.issues, 1)
        detail = "no data rows — review spec / instance path"
        errors.append(
            StepErrorLine(
                symptom=_symptom_from_detail(detail),
                subject="trace",
                tag="trace",
                detail=detail,
            )
        )
    elif kind == RUN_IO_TRACE:
        missing_rtl = sum(1 for r in rows if not (r.get("rtl") or "").strip())
        stats.issues = missing_rtl + header_errors
        if missing_rtl:
            detail = f"rows missing rtl: {missing_rtl}"
            errors.append(
                StepErrorLine(
                    symptom="rows missing rtl",
                    subject="trace",
                    tag="trace",
                    detail=detail,
                )
            )
    else:
        stats.issues = header_errors
    if header_errors and stats.total == 0:
        stats.issues = header_errors
        stats.total = header_errors
    return stats, errors


def summarize_suite_step_outcomes(
    entry: RunTestEntry,
    cfg: RunConfig,
    spec: Mapping[str, Any],
    *,
    work_dir: Path,
    base_dir: Path,
) -> StepOutcomeSummary:
    label = cfg.verification_step_name or entry.name or entry.kind
    artifacts, log_path = _artifact_paths_for_step(
        entry, cfg, work_dir=work_dir, base_dir=base_dir
    )
    resolved_arts = [_abs_path(p, base_dir=base_dir) for p in artifacts]
    resolved_log = (
        _abs_path(log_path, base_dir=base_dir) if log_path is not None else None
    )
    error_lines: List[StepErrorLine] = []
    stats: Optional[StepOutcomeStats] = None
    if entry.kind == RUN_CONN_CHECK:
        phase = (cfg.verification_phase or "logical").strip().lower()
        conn_art = _artifact_path_for_step(work_dir, cfg, phase=phase)
        stats, error_lines = _summarize_conn_outcomes(
            _abs_path(conn_art, base_dir=base_dir),
            phase=phase,
            spec=spec,
        )
    elif entry.kind == RUN_CONE_TRACE:
        stats, error_lines = _summarize_io_cone_outcomes(
            resolved_arts[0], kind=RUN_CONE_TRACE
        )
    elif entry.kind == RUN_IO_TRACE:
        stats, error_lines = _summarize_io_cone_outcomes(
            resolved_arts[0], kind=RUN_IO_TRACE
        )
    return StepOutcomeSummary(
        name=label,
        kind=entry.kind,
        artifacts=resolved_arts,
        log_path=resolved_log,
        errors=error_lines,
        stats=stats,
    )


def verify_suite_step_artifacts(
    entry: RunTestEntry,
    cfg: RunConfig,
    spec: Mapping[str, Any],
    *,
    work_dir: Path,
    document: Mapping[str, Any],
) -> List[StepIssue]:
    label = cfg.verification_step_name or entry.name or entry.kind
    phase = (cfg.verification_phase or "logical").strip().lower()
    issues: List[StepIssue] = []

    if entry.kind == RUN_CONN_CHECK:
        art = _artifact_path_for_step(work_dir, cfg, phase=phase)
        issues.extend(
            _validate_conn_tsv(
                art,
                phase=phase,
                expected=_expected_conn_outcomes(spec),
                step_label=label,
            )
        )
        hier = _hierarchy_path_for_conn(work_dir, cfg, phase=phase)
        issues.extend(
            _validate_hierarchy_tsv(hier, spec=spec, phase=phase, step_label=label)
        )
        log_p = _log_path_for_output(cfg.output, work_dir, phase=phase)
        issues.extend(_validate_log(log_p, step_label=label, phase=phase))
    elif entry.kind == RUN_CONE_TRACE:
        out = Path(cfg.output)
        issues.extend(_validate_cone_tsv(out, step_label=label))
        log_p = _log_path_for_output(cfg.output, work_dir)
        issues.extend(_validate_log(log_p, step_label=label))
    elif entry.kind == RUN_IO_TRACE:
        out = Path(cfg.output)
        issues.extend(_validate_io_trace_tsv(out, step_label=label))
        log_p = _log_path_for_output(cfg.output, work_dir)
        issues.extend(_validate_log(log_p, step_label=label))
    return issues


class _ArgParser:
    def error(self, msg: str) -> None:
        raise RuntimeError(msg)


def run_and_verify_suite(
    suite_path: Path,
    *,
    base_dir: Optional[Path] = None,
) -> SuiteVerifyReport:
    """Execute all enabled suite steps and validate artifacts + logs."""
    import os

    root = (base_dir or suite_path.parent).resolve()
    suite_path = suite_path.resolve()
    raw = suite_path.read_text(encoding="utf-8")
    document = json.loads(raw)
    suite = parse_run_test_suite(document, base_dir=root, raw_text=raw)
    plan = expand_suite_verification_plan(
        build_test_run_configs(suite, document, base_dir=root)
    )
    top = str(document.get("top") or suite.shared.top or "top")
    import getpass

    report = SuiteVerifyReport(
        suite_path=suite_path.resolve(),
        work_dir=resolve_run_work_dir(
            top,
            base=work_base_dir(str(root)),
            explicit_cache_dir=suite.shared.cache_dir,
        ),
        started_at=datetime.now().astimezone(),
        command=report_command_line(),
        cwd=root,
        user=getpass.getuser(),
    )
    ap = _ArgParser()
    prev_cwd = os.getcwd()
    os.chdir(root)
    try:
        return _execute_suite_plan(
            plan,
            document=document,
            report=report,
            ap=ap,
            suite_path=suite_path,
        )
    finally:
        os.chdir(prev_cwd)


def _execute_suite_plan(
    plan: Sequence[Tuple[RunTestEntry, RunConfig]],
    *,
    document: Mapping[str, Any],
    report: SuiteVerifyReport,
    ap: _ArgParser,
    suite_path: Optional[Path] = None,
) -> SuiteVerifyReport:
    if suite_path is not None and plan:
        archive_run_config_sources(
            report.work_dir,
            replace(
                plan[0][1],
                run_config_source=str(suite_path.resolve()),
            ),
        )
        import shutil

        archived = report.work_dir / suite_path.name
        if not archived.is_file() and suite_path.is_file():
            shutil.copy2(suite_path, archived)

    timing_rec = VerificationTimingRecorder(quiet=True)
    bind_suite_recorder(timing_rec)
    base_dir = (report.cwd or Path.cwd()).resolve()
    t0 = time.perf_counter()
    try:
        for entry, cfg in plan:
            if entry is None:
                continue
            label = cfg.verification_step_name or (entry.name if entry else "?")
            spec = spec_for_test_entry(document, entry) if entry else {}
            step_label = verification_step_label(cfg)
            if step_label is not None and entry is not None:
                kind = entry.kind
                name = entry.name or f"{entry.kind}[{entry.index}]"
                with verification_step(kind=kind, name=name, recorder=timing_rec):
                    rc = execute_run(cfg, ap)
            else:
                rc = execute_run(cfg, ap)
            report.steps_run += 1
            summary = summarize_suite_step_outcomes(
                entry,
                cfg,
                spec,
                work_dir=report.work_dir,
                base_dir=base_dir,
            )
            summary.execute_ok = rc == 0
            if rc != 0:
                report.steps_failed += 1
                report.issues.append(
                    StepIssue(label, "execute", f"execute_run returned {rc}")
                )
                summary.errors.insert(
                    0,
                    StepErrorLine(
                        symptom="execute failed",
                        subject="run",
                        tag="execute",
                        detail=f"execute_run returned {rc}",
                    ),
                )
                report.step_summaries.append(summary)
                continue
            if entry.kind in (RUN_CONN_CHECK, RUN_CONE_TRACE, RUN_IO_TRACE):
                report.issues.extend(
                    verify_suite_step_artifacts(
                        entry,
                        cfg,
                        spec,
                        work_dir=report.work_dir,
                        document=document,
                    )
                )
            report.step_summaries.append(summary)
    finally:
        bind_suite_recorder(None)
    report.elapsed_sec = time.perf_counter() - t0
    report.timing_steps = list(timing_rec.steps)
    timing_by_name = {s.name: s.elapsed_sec for s in timing_rec.steps}
    text_sec, logical_sec = connect_phase_timings(timing_rec.steps)
    for summary in report.step_summaries:
        hit = timing_by_name.get(summary.name)
        if hit is None:
            base = summary.name.split(":", 1)[0]
            hit = timing_by_name.get(base)
        if hit is not None:
            summary.elapsed_sec = hit
        elif summary.name.endswith(":text") and text_sec is not None:
            summary.elapsed_sec = text_sec
        elif summary.name.endswith(":logical") and logical_sec is not None:
            summary.elapsed_sec = logical_sec
    return report


def _format_artifact_list(paths: Sequence[Path]) -> str:
    if not paths:
        return "(none)"
    return ", ".join(str(p) for p in paths)


def _format_aligned_step_artifact_rows(
    summaries: Sequence[StepOutcomeSummary],
) -> List[str]:
    """Column-aligned step name, duration, and artifact basenames."""
    from hierwalk.progress import format_duration

    if not summaries:
        return []
    name_w = max(max(len(s.name) for s in summaries), len("Step"))
    time_w = max(
        max(len(format_duration(s.elapsed_sec)) for s in summaries),
        len("Time"),
    )
    lines = [f"  {'Step':<{name_w}}  {'Time':>{time_w}}  Artifacts"]
    for step in summaries:
        time_s = format_duration(step.elapsed_sec)
        if not step.artifacts:
            lines.append(f"  {step.name:<{name_w}}  {time_s:>{time_w}}  (none)")
            continue
        for i, art in enumerate(step.artifacts):
            name_col = step.name if i == 0 else ""
            time_col = time_s if i == 0 else ""
            lines.append(f"  {name_col:<{name_w}}  {time_col:>{time_w}}  {art.name}")
    return lines


def _format_step_timing_summary(report: SuiteVerifyReport) -> List[str]:
    from hierwalk.progress import format_duration

    text_sec, logical_sec = connect_phase_timings(report.timing_steps)
    steps_sum = sum(s.elapsed_sec for s in report.timing_steps)
    lines = ["--- summary ---"]
    lines.append(f"Result:        {'PASS' if report.ok else 'FAIL'}")
    lines.append(
        f"Steps:         {report.steps_run} run, {report.steps_failed} execute failure(s)"
    )
    lines.append(f"Issues:        {len(report.issues)} (verifier artifact/format/verdict)")
    lines.append(f"Total elapsed: {format_duration(report.elapsed_sec)}")
    if abs(report.elapsed_sec - steps_sum) > 0.05:
        lines.append(
            f"Step timings:  {format_duration(steps_sum)} "
            f"(recorded steps; excludes gaps between steps)"
        )
    lines.append("Connect phases (separate, not merged):")
    if text_sec is not None:
        lines.append(f"  text-conn:     {format_duration(text_sec)}")
    else:
        lines.append("  text-conn:     (not run)")
    if logical_sec is not None:
        lines.append(f"  logical-conn:  {format_duration(logical_sec)}")
    else:
        lines.append("  logical-conn:  (not run)")
    lines.append("Verification steps:")
    lines.extend(_format_aligned_step_artifact_rows(report.step_summaries))
    lines.append(f"Work-dir:      {report.work_dir.resolve()}")
    lines.append("")
    return lines


def _format_step_details(report: SuiteVerifyReport) -> List[str]:
    lines = ["--- details ---"]
    lines.append(
        "Per-step summary; Errors lists only failing rows (sorted by symptom)."
    )
    lines.append("")
    for step in report.step_summaries:
        lines.append(f"[{step.name}] kind={step.kind}")
        if step.log_path is not None:
            lines.append(f"  Log:       {step.log_path}")
        if step.artifacts:
            lines.append(f"  Artifacts: {_format_artifact_list(step.artifacts)}")
        if step.stats is not None:
            lines.append(f"  Issues:    {step.stats.ratio_line()}")
        elif not step.execute_ok:
            lines.append("  Issues:    execute failed")
        lines.extend(_format_sorted_error_lines(step.errors))
        lines.append("")
    if report.issues:
        lines.append("Verifier issues (artifact / format / verdict / coverage):")
        for issue in report.issues:
            lines.append(f"  [{issue.kind}] {issue.step}: {issue.message}")
    else:
        lines.append("Verifier issues: (none — artifact shape and expected verdicts OK)")
    lines.append("")
    return lines


def format_suite_verify_report(report: SuiteVerifyReport) -> str:
    lines: List[str] = []
    lines.extend(
        report_header_lines(
            argv=report.command.split() if report.command else None,
            cwd=report.cwd,
            user=report.user or None,
            when=report.started_at,
            suite_path=report.suite_path,
        )
    )
    lines.extend(_format_step_timing_summary(report))
    lines.extend(_format_step_details(report))
    lines.append("PASS" if report.ok else "FAIL")
    return "\n".join(lines) + "\n"