"""Zigzag comprehensive annex: vuln_plan + parse_matrix graft coverage."""

from __future__ import annotations

from hierwalk.vuln_plan import VULN_PLAN
from hierwalk.suite_conn_policy import CONN_LOGICAL_ONLY_NEGATIVE_IDS
from hierwalk.zigzag_annex_gen import (
    MATRIX_HIERARCHY_PATHS,
    vuln_annex_checks,
    vuln_logical_only_negative_ids,
    vuln_mapping_rows,
    vuln_structural_negative_ids,
)
from hierwalk.zigzag_torture_gen import TOP, _build_checks, _suite_conn_checks


def test_vuln_annex_covers_full_vuln_plan():
    checks = vuln_annex_checks(torture_top=TOP)
    assert len(checks) == len(VULN_PLAN)
    by_id = {c.check_id: c for c in checks}
    for spec in VULN_PLAN:
        cid = f"zz_vuln_{spec.case_id.lower()}"
        assert cid in by_id
        chk = by_id[cid]
        if spec.case_id == "B1":
            assert chk.endpoint_a == f"{TOP}.vuln_src_bind"
            assert chk.endpoint_b == f"{TOP}.u_vuln.u_b1.dst"
        elif spec.case_id == "A2a":
            assert chk.endpoint_a == f"{TOP}.u_vuln.u_e1.src"
            assert chk.endpoint_b == f"{TOP}.u_vuln.u_e1.dst"
        else:
            assert chk.endpoint_a.startswith(f"{TOP}.u_vuln.")
            assert chk.endpoint_b.startswith(f"{TOP}.u_vuln.")


def test_design_and_suite_include_annex_checks():
    design_ids = {c.check_id for c in _build_checks()}
    suite_ids = {c["id"] for c in _suite_conn_checks()}
    for spec in VULN_PLAN:
        cid = f"zz_vuln_{spec.case_id.lower()}"
        assert cid in design_ids
        assert cid in suite_ids
    assert "zz_matrix_hier_batch" in suite_ids


def test_logical_only_negative_covers_mask_and_opaque_cases():
    logical_only = vuln_logical_only_negative_ids()
    structural = vuln_structural_negative_ids()
    assert "zz_vuln_h3" in logical_only
    assert "zz_vuln_h8" in logical_only
    assert "zz_vuln_h9" in logical_only
    assert "zz_vuln_a1" in structural
    assert "zz_vuln_g3" in structural
    assert logical_only == CONN_LOGICAL_ONLY_NEGATIVE_IDS
    assert logical_only & structural == frozenset()


def test_mapping_rows_cover_vuln_and_matrix():
    rows = vuln_mapping_rows()
    vuln_rows = [r for r in rows if r["source"].startswith("vuln_plan")]
    matrix_rows = [r for r in rows if r["source"] == "parse_matrix"]
    assert len(vuln_rows) == len(VULN_PLAN)
    assert len(matrix_rows) == len(MATRIX_HIERARCHY_PATHS)