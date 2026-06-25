"""Suite verification report layout: artifacts per step and outcome details."""

from __future__ import annotations

from pathlib import Path

from hierwalk.suite_report_verify import (
    StepErrorLine,
    StepOutcomeStats,
    StepOutcomeSummary,
    SuiteVerifyReport,
    format_suite_verify_report,
)


def test_format_suite_report_lists_artifacts_and_step_outcomes():
    work = Path("/tmp/zz_work/.db_top")
    report = SuiteVerifyReport(
        suite_path=Path("/tmp/zz_work/zz.suite.json"),
        work_dir=work,
        steps_run=2,
        elapsed_sec=3.5,
        step_summaries=[
            StepOutcomeSummary(
                name="conn_text",
                kind="run_conn_check",
                elapsed_sec=1.2,
                artifacts=[
                    work / "zz_conn.text.tsv",
                    work / "zz_hierarchy.text.tsv",
                ],
                log_path=work / "zz_conn.text.hier-walk.log",
                stats=StepOutcomeStats(total=10, issues=2, label="checks"),
                errors=[
                    StepErrorLine(
                        symptom="no path",
                        subject="zz_clk_deep",
                        tag="review (JSON typo / RTL / endpoint?)",
                        detail="no path; 3 module(s)",
                    ),
                    StepErrorLine(
                        symptom="hierarchy not found",
                        subject="zz_missing_hierarchy",
                        tag="expected in logical only",
                        detail="hierarchy not found: u_missing",
                    ),
                ],
            ),
            StepOutcomeSummary(
                name="conn_logical",
                kind="run_conn_check",
                elapsed_sec=2.0,
                artifacts=[work / "zz_conn.tsv"],
                log_path=work / "zz_conn.hier-walk.log",
                stats=StepOutcomeStats(total=10, issues=0, label="checks"),
                errors=[],
            ),
        ],
        timing_steps=[],
    )
    body = format_suite_verify_report(report)
    assert "--- summary ---" in body
    assert "conn_text" in body
    assert "1.2s" in body
    assert "zz_conn.text.tsv" in body
    assert "Verification steps:" in body
    assert "Step" in body and "Time" in body and "Artifacts" in body
    assert "2/10 checks (20.0%)" in body
    assert "0/10 checks (0.0%)" in body
    assert "--- details ---" in body
    assert "Per-step summary" in body
    assert "Log:" in body
    assert "zz_conn.text.hier-walk.log" in body
    assert "  Errors:" in body
    assert "hierarchy not found | zz_missing_hierarchy" in body
    assert "no path | zz_clk_deep" in body
    # conn_logical has no errors — no Errors block under that step
    logical_block = body.split("[conn_logical]", 1)[1].split("\n\n", 1)[0]
    assert "  Errors:" not in logical_block
    assert body.index("--- summary ---") < body.index("--- details ---")