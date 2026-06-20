"""Regression harness for ad-hoc connectivity audit probes."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PROBE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "vuln_audit_probe.py"
_spec = importlib.util.spec_from_file_location("vuln_audit_probe", _PROBE_PATH)
assert _spec and _spec.loader
_probe = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _probe
_spec.loader.exec_module(_probe)

_PROBES = _probe.build_probes()
_FP_FN = [p for p in _PROBES if p.severity in ("FP", "FN")]
_ALL_LABELED = [p for p in _PROBES if p.severity in ("FP", "FN", "OK", "LIMIT")]


@pytest.mark.parametrize("probe", _FP_FN, ids=lambda p: p.case_id)
def test_audit_probe_fp_fn_matches_expectation(probe):
    got, ok, _note = _probe._run(probe)
    assert got == probe.expect, (
        f"{probe.case_id}: expected connected={probe.expect}, got {got}"
    )
    assert ok


@pytest.mark.parametrize("probe", _ALL_LABELED, ids=lambda p: p.case_id)
def test_audit_probe_all_labeled_pass(probe):
    _got, ok, _note = _probe._run(probe)
    assert ok