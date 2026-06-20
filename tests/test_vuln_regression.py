"""Vulnerability-class regression (random RTL + expected outcomes)."""

from __future__ import annotations

import pytest

from hierwalk.vuln_gen import format_vuln_report, run_vuln_batch, run_vuln_trial
from hierwalk.vuln_plan import VULN_PLAN, remediation_summary


def test_vuln_plan_has_all_groups():
    assert len(VULN_PLAN) >= 45
    assert any("branch_over_approx" in line for line in remediation_summary())


def test_vuln_single_trial_all_cases_exercised():
    _design, trial = run_vuln_trial(seed=42)
    assert trial.total == len(VULN_PLAN)
    assert trial.default_pass == trial.total
    assert trial.strict_pass == trial.total


@pytest.mark.stress
def test_vuln_batch_ten_trials():
    results = run_vuln_batch(trials=10, base_seed=424242)
    assert len(results) == 10
    for t in results:
        assert t.total == len(VULN_PLAN)
        assert t.default_pass == t.total, format_vuln_report([t])
        assert t.strict_pass == t.total, format_vuln_report([t])
    report = format_vuln_report(results)
    assert "per-case pass rate" in report