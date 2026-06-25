"""Zigzag torture flat-suite JSON: conn phases, hierarchy RTL, cone, io-trace."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hierwalk.connect_artifacts import connect_output_paths
from hierwalk.connect_expand import hierarchy_endpoint_specs, parse_list_display_spec
from hierwalk.run_tests import (
    build_test_run_configs,
    expand_suite_verification_plan,
    parse_run_test_suite,
)
from hierwalk.suite_report_verify import (
    format_suite_verify_report,
    run_and_verify_suite,
    verify_suite_step_artifacts,
)
from hierwalk.zigzag_torture_gen import (
    COLLISION,
    D1_SHADOW,
    DEEP_D5,
    R3_ALT,
    SHALLOW_R4,
    TOP,
    ZZ_COMMON_RTL,
    build_flat_suite_document,
    write_flat_suite_artifacts,
)


@pytest.fixture
def suite_bundle(tmp_path: Path):
    _fl, suite_path, design = write_flat_suite_artifacts(tmp_path / "zz_suite")
    return suite_path, design, tmp_path / "zz_suite"


def test_flat_suite_document_has_text_and_logical_conn(suite_bundle):
    suite_path, _design, root = suite_bundle
    doc = json.loads(suite_path.read_text(encoding="utf-8"))
    names = [t["name"] for t in doc["tests"]]
    assert "conn_text" in names
    assert "conn_logical" in names
    conn_steps = [
        t["run_conn_check"]
        for t in doc["tests"]
        if "run_conn_check" in t
    ]
    phases = {s["connect_phase"] for s in conn_steps}
    assert phases == {"text", "logical"}
    checks = conn_steps[0]["checks"]
    ids = {c["id"] for c in checks}
    assert "zz_list_display" in ids
    assert "zz_hier_array" in ids
    assert "zz_intentional_fail" in ids
    assert "zz_missing_hierarchy" in ids
    assert "zz_common_inst_batch" in ids
    assert "zz_common_inst_display" in ids
    assert "zz_bridge_d2_bus" in ids
    assert "zz_fake_deep_not_on_spine" in ids
    batch = next(c for c in checks if c["id"] == "zz_common_inst_batch")
    assert len(batch["expect_hierarchy"]) == 3
    cone_steps = [t for t in doc["tests"] if "run_cone_trace" in t]
    assert len(cone_steps) >= 4
    io_steps = [t for t in doc["tests"] if "run_io_trace" in t]
    assert len(io_steps) >= 5


def test_parse_suite_expands_conn_phases_not_cone_io(suite_bundle):
    suite_path, _design, root = suite_bundle
    doc = json.loads(suite_path.read_text(encoding="utf-8"))
    suite = parse_run_test_suite(doc, base_dir=root)
    plan = expand_suite_verification_plan(
        build_test_run_configs(suite, doc, base_dir=root)
    )
    conn_phases = [
        cfg.verification_phase
        for entry, cfg in plan
        if entry and entry.kind == "run_conn_check"
    ]
    assert conn_phases == ["text", "logical"]
    cone_count = sum(1 for entry, _ in plan if entry and entry.kind == "run_cone_trace")
    io_count = sum(1 for entry, _ in plan if entry and entry.kind == "run_io_trace")
    assert cone_count == 9  # includes cone_fanout_common_decoy (zz_common.v decoy)
    assert io_count == 7


def test_run_and_verify_zigzag_suite(suite_bundle):
    suite_path, design, root = suite_bundle
    report = run_and_verify_suite(suite_path, base_dir=root)
    if not report.ok:
        pytest.fail(format_suite_verify_report(report))
    work = report.work_dir
    conn_paths = connect_output_paths(work, "zz_conn.tsv")
    assert conn_paths.text_tsv.is_file()
    assert conn_paths.logical_tsv.is_file()
    hier_text = work / "zz_hierarchy.text.tsv"
    hier_logical = work / "zz_hierarchy.tsv"
    assert hier_text.is_file()
    assert hier_logical.is_file()
    hier_body = hier_text.read_text(encoding="utf-8")
    assert "[zz" not in hier_body
    assert DEEP_D5 in hier_body
    assert SHALLOW_R4 in hier_body
    for row_line in hier_body.splitlines()[1:]:
        if not row_line.strip():
            continue
        cols = row_line.split("\t")
        if len(cols) < 8:
            continue
        status, rtl = cols[4], cols[6]
        if status == "hit" and cols[2] == "inst":
            assert rtl.endswith(".v") or rtl.startswith("/")


def test_list_display_hierarchy_paths_not_bracket_blob(suite_bundle):
    suite_path, _design, root = suite_bundle
    doc = json.loads(suite_path.read_text(encoding="utf-8"))
    display = f"[{DEEP_D5}, {SHALLOW_R4}]"
    parsed = parse_list_display_spec(display)
    assert parsed == (DEEP_D5, SHALLOW_R4)
    for ep in parsed:
        specs = hierarchy_endpoint_specs(ep)
        assert specs == (ep,)
        assert not ep.startswith("[")


def test_multi_common_module_hierarchy_rtl(suite_bundle):
    """zz_common.v hosts multiple modules; hierarchy must map each inst correctly."""
    suite_path, design, root = suite_bundle
    report = run_and_verify_suite(suite_path, base_dir=root)
    hier_path = report.work_dir / "zz_hierarchy.text.tsv"
    assert hier_path.is_file()
    hier_text = hier_path.read_text(encoding="utf-8")
    headers = hier_text.splitlines()[0].split("\t")
    parsed = [
        dict(zip(headers, line.split("\t")))
        for line in hier_text.splitlines()[1:]
        if line.strip()
    ]
    common_hits = {
        (r["path"], r["module"])
        for r in parsed
        if r.get("status") == "hit"
        and ZZ_COMMON_RTL in r.get("rtl", "")
        and r.get("kind") == "inst"
    }
    assert (D1_SHADOW, "zz_decoy") in common_hits
    assert (COLLISION, "zz_collision_d") in common_hits
    assert (R3_ALT, "zz_decoy") in common_hits
    multi_module_issues = [i for i in report.issues if i.kind == "multi_module"]
    assert not multi_module_issues, multi_module_issues


def test_hierarchy_rtl_on_hit_nodes(suite_bundle):
    suite_path, design, root = suite_bundle
    doc = json.loads(suite_path.read_text(encoding="utf-8"))
    suite = parse_run_test_suite(doc, base_dir=root)
    plan = expand_suite_verification_plan(
        build_test_run_configs(suite, doc, base_dir=root)
    )
    text_entry, text_cfg = plan[0]
    assert text_cfg.verification_phase == "text"
    from hierwalk.cli_execute import execute_run

    class _Ap:
        def error(self, msg: str) -> None:
            raise AssertionError(msg)

    rc = execute_run(text_cfg, _Ap())
    assert rc == 0
    spec = doc["tests"][0]["run_conn_check"]
    issues = verify_suite_step_artifacts(
        text_entry,
        text_cfg,
        spec,
        work_dir=root / f".db_{design.top}",
        document=doc,
    )
    bracket_issues = [i for i in issues if i.kind == "bracket"]
    rtl_issues = [i for i in issues if i.kind == "rtl"]
    assert not bracket_issues, bracket_issues
    assert not rtl_issues, rtl_issues