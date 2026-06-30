"""End-of-run summary report for hier-walk."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence, TextIO, Tuple

from hierwalk.coverage_audit import CoverageAuditResult
from hierwalk.filelist import FilelistResult
from hierwalk.index import DesignIndex
from hierwalk.hierarchy_log import format_hierarchy_rows_report
from hierwalk.models import ConnectResult, FlatRow, SearchHit
from hierwalk.path_chain import format_path_chain_report
from hierwalk.progress import format_duration, format_hierwalk_log
from hierwalk.report_provenance import format_timing_summary_lines, report_header_lines


def format_bytes(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    if num < 1024 * 1024 * 1024:
        return f"{num / (1024 * 1024):.1f} MB"
    return f"{num / (1024 * 1024 * 1024):.2f} GB"


def cache_file_size(path: Optional[Path]) -> Optional[int]:
    if path is None or not path.is_file():
        return None
    try:
        return path.stat().st_size
    except OSError:
        return None


def index_body_bytes(index: DesignIndex) -> int:
    return sum(len(rec.body) for rec in index.modules.values())


@dataclass
class RunReport:
    filelist_path: str
    elapsed_sec: float
    fl: FilelistResult
    index: DesignIndex
    cache_path: Optional[Path] = None
    cache_enabled: bool = True
    index_cache_hit: bool = False
    index_rebuilt: bool = False
    index_incremental: bool = False
    elab_tops: Sequence[str] = ()
    elab_cache_hits: int = 0
    instance_rows: int = 0
    search_hits: Optional[int] = None
    search_pattern: Optional[str] = None
    search_hit_details: Sequence[SearchHit] = ()
    top_candidates: Optional[int] = None
    mode: str = "hierarchy"
    output_path: str = "-"
    filelist_warnings: int = 0
    coverage: Optional[CoverageAuditResult] = None
    hierarchy_rows: Sequence[FlatRow] = ()
    connect_results: Sequence[ConnectResult] = ()
    connect_phase: str = ""
    connect_rows_by_path: Mapping[str, FlatRow] = field(default_factory=dict)
    connect_signal_tails: Sequence[object] = ()
    connect_top: str = ""
    report_argv: Optional[Sequence[str]] = None
    report_cwd: Optional[Path] = None
    report_user: Optional[str] = None
    report_started_at: Optional[datetime] = None

    def lines(self) -> List[str]:
        out: List[str] = []
        out.extend(
            report_header_lines(
                argv=self.report_argv,
                cwd=self.report_cwd,
                user=self.report_user,
                when=self.report_started_at,
            )
        )
        phase = (self.connect_phase or "").strip().lower()
        timing_steps = []
        if phase in ("text", "logical", "both"):
            from hierwalk.verification_timing import get_active_recorder

            rec = get_active_recorder()
            if rec is not None and rec.steps:
                timing_steps = list(rec.steps)
        if timing_steps:
            out.extend(
                format_timing_summary_lines(
                    timing_steps,
                    wall_sec=self.elapsed_sec,
                    ok=None,
                )
            )
        else:
            out.append("--- summary ---")
            out.append(f"Elapsed:       {format_duration(self.elapsed_sec)}")
            if phase in ("text", "logical"):
                label = "text-conn" if phase == "text" else "logical-conn"
                out.append(f"Connect phase: {label} ({format_duration(self.elapsed_sec)})")
            out.append("")
        out.append("--- details ---")
        out.append(f"Mode:          {self.mode}")
        out.append(f"Filelist:      {self.filelist_path}")
        if self.fl.index_cwd_used:
            out.append(f"Index cwd:     {self.fl.index_cwd_used}")
        if self.output_path != "-":
            out.append(f"Output:        {self.output_path}")

        out.append("")
        out.append("Inventory")
        out.append(f"  RTL sources:   {len(self.fl.source_files)}")
        out.append(f"  Modules:       {len(self.index.modules)}")
        out.append(f"  Filelists:     {len(self.fl.filelist_info)}")
        out.append(f"  Include dirs:  {len(self.fl.include_dirs)}")
        out.append(f"  Defines:       {len(self.fl.defines)}")
        if self.fl.library_files or self.fl.library_dirs:
            out.append(
                f"  Libraries:     {len(self.fl.library_files)} -v, "
                f"{len(self.fl.library_dirs)} -y"
            )

        body_b = index_body_bytes(self.index)
        out.append(f"  Index body:    {format_bytes(body_b)} (in memory)")

        out.append("")
        out.append("Filelist linking")
        edges = self._sorted_filelist_edges()
        if not edges:
            out.append("  (none)")
        else:
            for parent, child, kind in edges:
                parent_name = Path(parent).name
                child_name = Path(child).name
                out.append(f"  {parent_name} {kind} {child_name}")

        out.append("")
        out.append("Index cache")
        if not self.cache_enabled:
            out.append("  Status:        disabled (--no-cache)")
        else:
            if self.index_cache_hit:
                status = "hit (loaded)"
            elif self.index_incremental:
                status = "incremental"
            elif self.index_rebuilt:
                status = "miss (rebuilt)"
            else:
                status = "miss"
            out.append(f"  Status:        {status}")
            if self.cache_path is not None:
                out.append(f"  Path:          {self.cache_path}")
            size = cache_file_size(self.cache_path)
            if size is not None:
                out.append(f"  Size:          {format_bytes(size)}")
            elab_cached = self.elab_cache_hits
            elab_total = len(self.elab_tops)
            if elab_total:
                out.append(f"  Elab cached:   {elab_cached}/{elab_total} tops")

        out.append("")
        out.append("Execution")
        out.append(f"  Index workers: {self.index.index_jobs}")
        if self.top_candidates is not None:
            out.append(f"  Top candidates:{self.top_candidates}")
        if self.elab_tops:
            tops = ", ".join(self.elab_tops)
            out.append(f"  Elab tops:     {tops}")
        if self.instance_rows:
            out.append(f"  Instances:     {self.instance_rows}")
        if self.hierarchy_rows:
            out.append("")
            cap = 200 if len(self.hierarchy_rows) > 200 else None
            out.extend(
                format_hierarchy_rows_report(
                    self.hierarchy_rows,
                    limit=cap,
                    title="Hierarchy (rtl + filelist per node)",
                )
            )
        if self.search_hits is not None:
            pat = self.search_pattern or ""
            out.append(f"  Search:        {self.search_hits} hits ({pat!r})")

        if self.connect_results or self.connect_phase:
            from hierwalk.connectivity import format_connect_results_report

            phase = (self.connect_phase or "logical").strip().lower()
            if phase not in ("text", "logical", "both"):
                phase = "logical"
            out.append("")
            out.append("Connectivity (hierarchy analysis)")
            if phase in ("text", "logical"):
                out.append(f"  Phase:         {phase}")
            out.append("  Elements:      inst / port / wire / reg (hit or miss)")
            tops = list(self.elab_tops)
            top_label = self.connect_top or (tops[0] if tops else "")
            out.extend(
                format_connect_results_report(
                    self.connect_results,
                    phase=phase,
                    rows_by_path=self.connect_rows_by_path,
                    signal_tails=self.connect_signal_tails,
                    index=self.index,
                    top=top_label,
                )
            )

        mapped_hits = [h for h in self.search_hit_details if h.path_chain]
        if mapped_hits:
            out.append("")
            out.append("Search path mapping")
            for hit in mapped_hits:
                out.append(f"  {hit.full_path}")
                out.extend(format_path_chain_report(hit.path_chain))

        if self.filelist_warnings:
            out.append("")
            out.append(f"Warnings:      {self.filelist_warnings} filelist issue(s)")

        if self.coverage is not None:
            out.append("")
            out.extend(self.coverage.summary_lines())

        out.append("---")
        return out

    def _sorted_filelist_edges(self) -> List[Tuple[str, str, str]]:
        edges = list(self.fl.filelist_edges)
        edges.sort(key=lambda e: (Path(e[0]).name, e[2], Path(e[1]).name))
        return edges


def _log_basename(stem: str, *, phase: str = "") -> str:
    phase_label = str(phase).strip().lower()
    if phase_label == "text":
        return f"{stem}.text.hier-walk.log"
    return f"{stem}.hier-walk.log"


def phase_log_path(path: Path, *, phase: str = "") -> Path:
    """Apply text-conn / logical-conn suffix to an explicit log path."""
    phase_label = str(phase).strip().lower()
    resolved = path.expanduser().resolve()
    if phase_label != "text":
        return resolved
    name = resolved.name
    if name.endswith(".hier-walk.log"):
        return resolved.with_name(
            name[: -len(".hier-walk.log")] + ".text.hier-walk.log"
        )
    if resolved.suffix:
        return resolved.with_name(f"{resolved.stem}.text{resolved.suffix}")
    return Path(f"{resolved}.text.log")


def default_log_path(
    filelist_path: str,
    output_path: str = "-",
    *,
    work_dir: Optional[Path] = None,
    phase: str = "",
) -> Path:
    """Default report log under per-top work dir, or legacy beside output/filelist."""
    if work_dir is not None:
        stem = (
            Path(output_path).stem
            if output_path != "-"
            else (Path(filelist_path).stem if filelist_path else "run")
        )
        return work_dir / _log_basename(stem, phase=phase)
    if output_path != "-":
        out = Path(output_path).resolve()
        phase_label = str(phase).strip().lower()
        if phase_label == "text":
            return out.parent / f"{out.stem}.text.hier-walk.log"
        return out.parent / f"{out.name}.hier-walk.log"
    fl = Path(filelist_path).resolve()
    return fl.parent / _log_basename(fl.stem, phase=phase)


def _fill_report_provenance(report: RunReport) -> RunReport:
    import getpass
    import os
    import sys

    if report.report_argv is None:
        report.report_argv = list(sys.argv)
    if report.report_cwd is None:
        report.report_cwd = Path.cwd()
    if report.report_user is None:
        report.report_user = getpass.getuser()
    if report.report_started_at is None:
        report.report_started_at = datetime.now().astimezone()
    return report


def write_run_report_log(
    report: RunReport,
    log_path: Path,
    *,
    append: bool = True,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines = _fill_report_provenance(report).lines()
    mode = "a" if append else "w"
    with log_path.open(mode, encoding="utf-8") as fh:
        if append:
            fh.write("\n")
        fh.write("\n".join(lines))
        fh.write("\n")


def emit_run_report(
    report: RunReport,
    *,
    stream: Optional[TextIO] = None,
    log_path: Optional[Path] = None,
    append_log: bool = True,
    announce_log: bool = True,
) -> Optional[Path]:
    """Print report to stderr and optionally append to a log file."""
    target = stream or sys.stderr
    for line in _fill_report_provenance(report).lines():
        print(line, file=target, flush=True)
    if log_path is None:
        return None
    write_run_report_log(report, log_path, append=append_log)
    if announce_log:
        print(format_hierwalk_log(f"report logged: {log_path}"), file=target, flush=True)
    return log_path


def print_run_report(
    report: RunReport,
    *,
    stream: Optional[TextIO] = None,
) -> None:
    emit_run_report(report, stream=stream, log_path=None, announce_log=False)