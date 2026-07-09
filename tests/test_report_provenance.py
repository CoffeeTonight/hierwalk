"""Report header and timing summary layout."""

from __future__ import annotations

from datetime import datetime, timezone

from hierwalk.report_provenance import (
    connect_phase_timings,
    format_timing_summary_lines,
    report_header_lines,
)
from hierwalk.verification_timing import StepTiming


def test_report_header_contains_tool_cwd_command_user():
    when = datetime(2026, 6, 26, 12, 30, 0, tzinfo=timezone.utc)
    lines = report_header_lines(
        argv=["hier-walk", "suite.json", "--run-test-suite"],
        cwd="/tmp/work",
        user="tester",
        when=when,
        suite_path="/tmp/work/zz.suite.json",
    )
    text = "\n".join(lines)
    assert "hier-walk" in text
    assert "tester" in text
    assert "/tmp/work" in text
    assert "suite.json" in text
    assert "zz.suite.json" in text
    assert "2026-06-26" in text


def test_timing_summary_lists_text_and_logical_separately():
    steps = [
        StepTiming(kind="run_conn_check", name="conn_text", elapsed_sec=1.2),
        StepTiming(kind="run_conn_check", name="conn_logical", elapsed_sec=2.3),
        StepTiming(kind="run_io_trace", name="io_trace_ff", elapsed_sec=0.4),
    ]
    text_sec, logical_sec, hgrep_sec = connect_phase_timings(steps)
    assert text_sec == 1.2
    assert logical_sec == 2.3
    assert hgrep_sec is None

    lines = format_timing_summary_lines(
        steps,
        wall_sec=4.5,
        steps_run=3,
        steps_failed=0,
        issue_count=0,
        ok=True,
    )
    body = "\n".join(lines)
    assert "--- summary ---" in body
    assert "Result:        PASS" in body
    assert "Total elapsed: 4.5s" in body
    assert "grep-hierarchy: (not run)" in body
    assert "text-conn:     1.2s" in body
    assert "logical-conn:  2.3s" in body


def test_timing_summary_includes_grep_hierarchy_phase():
    steps = [
        StepTiming(kind="run_conn_check", name="run_conn_check[0]:hgrep", elapsed_sec=0.85),
    ]
    text_sec, logical_sec, hgrep_sec = connect_phase_timings(steps)
    assert hgrep_sec == 0.85
    assert text_sec is None
    assert logical_sec is None
    lines = format_timing_summary_lines(steps, wall_sec=0.9)
    body = "\n".join(lines)
    assert "grep-hierarchy: 850ms" in body
    assert "text-conn:     (not run)" in body
    assert "logical-conn:  (not run)" in body