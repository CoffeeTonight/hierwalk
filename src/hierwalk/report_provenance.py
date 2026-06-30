"""Report header, summary-first layout, and verification timing breakdown."""

from __future__ import annotations

import getpass
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple

from hierwalk.progress import format_duration

_TOOL_NAME = "hier-walk"


def _tool_version() -> str:
    import hierwalk

    return str(getattr(hierwalk, "__version__", "unknown"))


def report_command_line(argv: Optional[Sequence[str]] = None) -> str:
    args = list(argv if argv is not None else sys.argv)
    if not args or (len(args) == 1 and not str(args[0]).strip()):
        return f"{sys.executable} <suite verification>"
    return shlex.join(args)


def report_header_lines(
    *,
    argv: Optional[Sequence[str]] = None,
    cwd: Optional[Path] = None,
    user: Optional[str] = None,
    when: Optional[datetime] = None,
    suite_path: Optional[Path] = None,
) -> List[str]:
    """Provenance block printed at the top of every report."""
    resolved_cwd = Path(cwd).resolve() if cwd is not None else Path.cwd().resolve()
    stamp = when or datetime.now().astimezone()
    uid = user or getpass.getuser()
    lines = [
        f"=== {_TOOL_NAME} report ===",
        f"Tool:          {_TOOL_NAME} {_tool_version()}",
        f"User:          {uid}",
        f"Started:       {stamp.strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"Working dir:   {resolved_cwd}",
        f"Command:       {report_command_line(argv)}",
    ]
    if suite_path is not None:
        lines.append(f"Suite:         {Path(suite_path).resolve()}")
    lines.append("")
    return lines


def _step_blob(step) -> str:
    return f"{step.name} {step.kind}".lower()


def classify_connect_timing_phase(step) -> Optional[str]:
    """Return ``text`` or ``logical`` when *step* is a connect timing step."""
    blob = _step_blob(step)
    if "text-conn" in blob or blob.startswith("conn_text") or " conn_text" in blob:
        return "text"
    if "logical-conn" in blob or blob.startswith("conn_logical") or " conn_logical" in blob:
        return "logical"
    if step.kind == "connect-coi" or step.name == "text-conn":
        return "text"
    if step.name == "logical-conn" or step.kind == "activation-audit":
        return "logical"
    if step.kind == "run_conn_check":
        if "text" in blob:
            return "text"
        if "logical" in blob:
            return "logical"
    return None


def connect_phase_timings(
    steps: Sequence,
) -> Tuple[Optional[float], Optional[float]]:
    """Return (text_sec, logical_sec); each phase uses the longest matching step."""
    text_best = 0.0
    logical_best = 0.0
    text_hit = False
    logical_hit = False
    for step in steps:
        phase = classify_connect_timing_phase(step)
        if phase == "text" and step.elapsed_sec >= text_best:
            text_best = step.elapsed_sec
            text_hit = True
        elif phase == "logical" and step.elapsed_sec >= logical_best:
            logical_best = step.elapsed_sec
            logical_hit = True
    return (text_best if text_hit else None, logical_best if logical_hit else None)


def format_timing_summary_lines(
    steps: Sequence,
    *,
    wall_sec: Optional[float] = None,
    steps_run: Optional[int] = None,
    steps_failed: Optional[int] = None,
    issue_count: Optional[int] = None,
    ok: Optional[bool] = None,
) -> List[str]:
    """Summary block: verdict, wall clock, text/logical conn listed separately."""
    text_sec, logical_sec = connect_phase_timings(steps)
    other_steps = [
        step
        for step in steps
        if classify_connect_timing_phase(step) is None
    ]
    steps_sum = sum(step.elapsed_sec for step in steps)
    total = wall_sec if wall_sec is not None else steps_sum

    lines = ["--- summary ---"]
    if ok is not None:
        lines.append(f"Result:        {'PASS' if ok else 'FAIL'}")
    if steps_run is not None:
        failed = steps_failed or 0
        lines.append(f"Steps:         {steps_run} run, {failed} execute failure(s)")
    if issue_count is not None:
        lines.append(f"Issues:        {issue_count}")
    lines.append(f"Total elapsed: {format_duration(total)}")
    if wall_sec is not None and abs(wall_sec - steps_sum) > 0.05:
        lines.append(
            f"Step timings:  {format_duration(steps_sum)} "
            f"(recorded steps; excludes gaps between steps)"
        )
    lines.append("Connect phases (reported separately, not merged):")
    if text_sec is not None:
        lines.append(f"  text-conn:     {format_duration(text_sec)}")
    else:
        lines.append("  text-conn:     (not run)")
    if logical_sec is not None:
        lines.append(f"  logical-conn:  {format_duration(logical_sec)}")
    else:
        lines.append("  logical-conn:  (not run)")
    if other_steps:
        lines.append("Other verification steps:")
        for step in other_steps:
            lines.append(
                f"  {step.name} ({step.kind}): {format_duration(step.elapsed_sec)}"
            )
    lines.append("")
    return lines