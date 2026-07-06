"""Zigzag torture flat-suite JSON: conn phases, hierarchy RTL, cone, io-trace."""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hierwalk.connect.pipeline.artifacts import connect_output_paths
from hierwalk.connect.shared.expand import (
    build_expand_meta,
    hierarchy_endpoint_specs,
    parse_list_display_spec,
)
from hierwalk.run_tests import (
    RUN_CONN_CHECK,
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
    DEEP_D2,
    DEEP_D4,
    DEEP_D5,
    DW_VENDOR_RTL,
    R3_ALT,
    SCOPE_ARM,
    SCOPE_B,
    SHALLOW_R4,
    TOP,
    ZZ_ANNEX_FL_DIR,
    ZZ_COLLISION_D_RTL,
    ZZ_DECOY_RTL,
    ZZ_IFNDEF_PING_RTL,
    ZZ_LIB_FL_DIR,
    ZZ_SCOPE_A_RTL,
    ZZ_SCOPE_FL_DIR,
    ZZ_SPINE_FL_DIR,
    ZZ_SCOPE_B_STUB_RTL,
    ZZ_SCOPE_C_STUB_RTL,
    ZZ_SCOPE_A_STUB_RTL,
    ZZ_SCOPE_DECOY_RTL,
    ZZ_SCOPE_STUB_RTLS,
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
    assert "zz_dw_vendor_ignored" in ids
    assert "zz_fanin_merge" in ids
    assert "zz_fanin_merge_decoy" in ids
    assert "zz_port_expr_xor" in ids
    assert "zz_casex_route" in ids
    assert "zz_loop_range" in ids
    assert "zz_bb_through" in ids
    assert "zz_ifdef_inactive" in ids
    assert "zz_gen_tap1" in ids
    assert "zz_pong_replicate" in ids
    assert "zz_ff_barrier_tap" in ids
    assert "zz_multi_g3_empty" in ids
    assert "zz_ifndef_define_mix" in ids
    assert "zz_scope_confident_b" in ids
    assert "zz_ifdef_nested_u_b" in ids
    assert "zz_scope_b_to_c" in ids
    assert "zz_scope_c_to_b" in ids
    for idx in range(10):
        assert f"zz_ifdef_var_{idx:02d}" in ids
    assert "zz_dw_vendor_inst" not in ids
    multi_g3 = next(c for c in checks if c["id"] == "zz_multi_g3_empty")
    assert multi_g3.get("expect_connected") is False
    scope_chk = next(c for c in checks if c["id"] == "zz_scope_confident_b")
    assert scope_chk.get("expect_connected") is True
    assert len(scope_chk["expect_hierarchy"]) == 3
    assert len(checks) == 119
    assert sum(1 for c in checks if c.get("expect_connected") is True) >= 70
    assert "zz_vuln_a1" in ids
    assert "zz_vuln_b1" in ids
    assert "zz_matrix_hier_batch" in ids
    by_id = {c["id"]: c for c in checks}
    assert by_id["zz_vuln_a1"]["expect_connected"] is False
    assert by_id["zz_vuln_d3"]["expect_connected"] is True
    for step in conn_steps:
        assert step["ignore-path"] == ["DW_*"]
    batch = next(c for c in checks if c["id"] == "zz_common_inst_batch")
    assert len(batch["expect_hierarchy"]) == 3
    cone_steps = [t for t in doc["tests"] if "run_cone_trace" in t]
    assert len(cone_steps) >= 4
    io_steps = [t for t in doc["tests"] if "run_io_trace" in t]
    assert len(io_steps) >= 5


def test_round17_conn_check_shapes(suite_bundle):
    """회차17: fan-in merge + port-expr XOR check JSON semantics."""
    suite_path, _design, root = suite_bundle
    doc = json.loads(suite_path.read_text(encoding="utf-8"))
    checks = doc["tests"][0]["run_conn_check"]["checks"]
    by_id = {c["id"]: c for c in checks}

    fanin = by_id["zz_fanin_merge"]
    assert fanin["expect_connected"] is True
    ep_a = tuple(fanin["a"])
    ep_b = fanin["b"]
    assert build_expand_meta(ep_a, ep_b).map_kind == "fanout"

    decoy = by_id["zz_fanin_merge_decoy"]
    assert decoy["expect_connected"] is False
    assert decoy["a"] == f"{DEEP_D4}.fork_decoy[1][2]"
    assert decoy["b"] == f"{DEEP_D4}.merge_tap"

    xor_chk = by_id["zz_port_expr_xor"]
    assert xor_chk["expect_connected"] is True
    assert tuple(xor_chk["a"]) == (
        f"{DEEP_D2}.chain_in[1][2]",
        f"{DEEP_D2}.shallow_return[1][2]",
    )
    assert xor_chk["b"] == f"{DEEP_D2}.u_bridge_expr.din[1][2]"
    assert build_expand_meta(tuple(xor_chk["a"]), xor_chk["b"]).map_kind == "fanout"
    assert f"{DEEP_D4}.chain_in[1][2]" in fanin["a"]


def test_round21_scope_rtl_and_filelist_order(suite_bundle):
    suite_path, design, root = suite_bundle
    assert "zz_scope_A u_a" in design.files["zz_torture_top.v"]
    assert "module zz_scope_A" in design.files[ZZ_SCOPE_DECOY_RTL]
    scope_a = design.files[ZZ_SCOPE_A_RTL]
    assert "zz_scope_B u_b" in scope_a
    assert "zz_scope_C u_c" in scope_a
    assert "module zz_scope_B" not in scope_a
    assert "zz_b_to_c" in scope_a
    assert "////////" in scope_a
    assert "`ifndef ZZ_SCOPE_NO_B" in scope_a
    assert "`ifdef ZZ_SCOPE_USE_ALT" in scope_a
    assert "/*" in scope_a and "ZZ_SCOPE_FAKE_BLK" in scope_a
    assert "module zz_scope_B" in design.files["zz_scope_b.v"]
    assert "module zz_scope_C" in design.files["zz_scope_c.v"]
    assert "module zz_scope_v00" in design.files["zz_scope_v00.v"]
    assert "module zz_scope_v09" in design.files["zz_scope_v09.v"]
    assert "zz_scope_v00 u_av00" in design.files["zz_torture_top.v"]
    assert "module zz_scope_B;" in design.files[ZZ_SCOPE_B_STUB_RTL]
    assert "module zz_scope_A;" in design.files[ZZ_SCOPE_A_STUB_RTL]
    assert "module zz_scope_C;" in design.files[ZZ_SCOPE_C_STUB_RTL]
    fl_lines = (root / "filelist.f").read_text(encoding="utf-8").splitlines()
    decoy = str((root / ZZ_SCOPE_DECOY_RTL).resolve())
    real_a = str((root / ZZ_SCOPE_A_RTL).resolve())
    real_b = str((root / "zz_scope_b.v").resolve())
    real_c = str((root / "zz_scope_c.v").resolve())
    real_v00 = str((root / "zz_scope_v00.v").resolve())
    assert fl_lines.index(decoy) < fl_lines.index(real_a)
    assert real_b not in fl_lines
    assert real_c not in fl_lines
    assert real_v00 not in fl_lines
    assert str((root / "zz_deep_d2.v").resolve()) not in fl_lines
    assert str((root / ZZ_DECOY_RTL).resolve()) not in fl_lines
    fl_dir = root / ZZ_SCOPE_FL_DIR
    parent_fl = str((fl_dir / "parent.f").resolve())
    nested_roots = [
        str((root / ZZ_SCOPE_FL_DIR / "parent.f").resolve()),
        str((root / ZZ_LIB_FL_DIR / "root.f").resolve()),
        str((root / ZZ_SPINE_FL_DIR / "root.f").resolve()),
        str((root / ZZ_ANNEX_FL_DIR / "root.f").resolve()),
    ]
    assert sum(1 for line in fl_lines if line.strip() in {f"-f {p}" for p in nested_roots}) == 4
    assert any(line.strip() == f"-f {parent_fl}" for line in fl_lines)
    assert (fl_dir / "b.f").read_text(encoding="utf-8").splitlines()[0] == real_b
    assert (fl_dir / "c.f").read_text(encoding="utf-8").splitlines()[0] == real_c
    assert (fl_dir / "v00.f").read_text(encoding="utf-8").splitlines()[0] == real_v00
    stub_text = (fl_dir / "stub.f").read_text(encoding="utf-8")
    for stub_rtl in ZZ_SCOPE_STUB_RTLS:
        assert stub_rtl in stub_text
    assert "-f" in (fl_dir / "b.f").read_text(encoding="utf-8")
    assert "-f" in (fl_dir / "v09.f").read_text(encoding="utf-8")


def test_round18_rtl_probes_in_generated_files(suite_bundle):
    suite_path, design, _root = suite_bundle
    assert "u_bridge_expr" in design.files["zz_deep_d2.v"]
    assert "u_ifndef_mix" in design.files["zz_deep_d2.v"]
    assert "`ifndef ZZ_IFNDEF_INST_" in design.files["zz_deep_d2.v"]
    assert "`ifndef ZZ_IFNDEF_PING_BODY_" in design.files[ZZ_IFNDEF_PING_RTL]
    assert "chain_in ^ shallow_return" in design.files["zz_deep_d2.v"]
    assert "assign merge_tap" in design.files["zz_deep_d4.v"]
    assert "u_bridge_concat" in design.files["zz_deep_d2.v"]
    assert "gen_pass_flat" in design.files["zz_deep_d5.v"]
    assert "gen_tap0" in design.files["zz_deep_d1.v"]
    assert "u_dw_vendor" in design.files["zz_torture_top.v"]
    assert "assign dout = din" in design.files[ZZ_IFNDEF_PING_RTL]


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
    assert cone_count == 20
    assert io_count == 15


def test_conn_ignore_path_dw_glob_merge_and_pw_db(suite_bundle):
    """run_conn_check ignore-path DW_* must survive run_on_full_index ignore-path: []."""
    suite_path, design, root = suite_bundle
    doc = json.loads(suite_path.read_text(encoding="utf-8"))
    merge_doc = {
        **doc,
        "run_on_full_index": {
            "enable": 1,
            "mode": "hierarchy",
            "ignore-path": [],
            "output": "zz_instances.tsv",
        },
    }
    suite = parse_run_test_suite(merge_doc, base_dir=root)
    conn_cfg = next(
        cfg
        for ent, cfg in build_test_run_configs(suite, merge_doc, base_dir=root)
        if ent.kind == RUN_CONN_CHECK
    )
    assert conn_cfg.ignore_path == ("DW_*",)

    suite = parse_run_test_suite(doc, base_dir=root)
    conn_cfg = next(
        cfg
        for ent, cfg in build_test_run_configs(suite, doc, base_dir=root)
        if ent.kind == RUN_CONN_CHECK
    )
    assert conn_cfg.ignore_path == ("DW_*",)

    from hierwalk.filelist import parse_filelist
    from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
    from hierwalk.path_walk import run_path_walk_connect

    flr = parse_filelist(str(root / "filelist.f"), index_cwd=str(root))
    req = ConnectivityRequest(
        checks=(ConnectivityCheck(f"{TOP}.clk", f"{TOP}.clk", check_id="zz_dw_vendor_ignored"),),
        top=design.top,
        defines=doc.get("defines") or {},
    )
    import io

    trace = io.StringIO()
    _batch, _index, state = run_path_walk_connect(
        req,
        flr,
        top=design.top,
        extra_defines=req.defines,
        no_cache=True,
        ignore_paths=list(conn_cfg.ignore_path),
        trace_stream=trace,
    )
    dw_vendor = str((root / DW_VENDOR_RTL).resolve())
    assert dw_vendor not in state.mod_db._sources
    assert DW_VENDOR_RTL not in [Path(p).name for p in state.mod_db._regex_scanned]
    trace_text = trace.getvalue()
    assert "tier0 cache DW_" not in trace_text
    assert "tier0 scan DW_" not in trace_text


@pytest.mark.slow
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

    headers = hier_body.splitlines()[0].split("\t")
    parsed = [
        dict(zip(headers, line.split("\t")))
        for line in hier_body.splitlines()[1:]
        if line.strip()
    ]
    library_rtl_hits = {
        (r["path"], r["module"])
        for r in parsed
        if r.get("status") == "hit"
        and any(
            rtl_name in r.get("rtl", "")
            for rtl_name in (ZZ_DECOY_RTL, ZZ_COLLISION_D_RTL)
        )
        and r.get("kind") == "inst"
    }
    assert (D1_SHADOW, "zz_decoy") in library_rtl_hits
    assert (COLLISION, "zz_collision_d") in library_rtl_hits
    assert (R3_ALT, "zz_decoy") in library_rtl_hits
    multi_module_issues = [i for i in report.issues if i.kind == "multi_module"]
    assert not multi_module_issues, multi_module_issues

    if os.environ.get("HIERWALK_ARCHIVE_SUITE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        archive_root = Path.home() / "tools" / "zz_suite_artifacts"
        archive_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = archive_root / f"run_{stamp}"
        shutil.copytree(work, dest, dirs_exist_ok=False)
        for name in (
            "zz_conn.text.tsv",
            "zz_conn.tsv",
            "zz_hierarchy.text.tsv",
            "zz_hierarchy.tsv",
            "zz_conn.text.hier-walk.log",
            "zz_conn.hier-walk.log",
        ):
            assert (dest / name).is_file(), name
        log_path = archive_root / f"run_{stamp}.meta.json"
        log_path.write_text(
            json.dumps(
                {
                    "python": sys.executable,
                    "elapsed_sec": report.elapsed_sec,
                    "steps_run": report.steps_run,
                    "issues": len(report.issues),
                    "work_dir": str(dest),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


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


def test_scope_confident_b_resolves_without_recovery(suite_bundle, monkeypatch):
    """Regression anchor: nested child FL + co-list order must resolve ``u_a.u_b``."""
    suite_path, design, root = suite_bundle
    from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
    from hierwalk.filelist import parse_filelist
    from hierwalk.path_walk import run_path_walk_connect

    def _no_recovery(self, spec_targets=None, **kwargs):
        return 0, 0, []

    monkeypatch.setattr(
        "hierwalk.path_walk.PathWalkState.run_recovery_pass",
        _no_recovery,
    )
    fl = parse_filelist(str(root / "filelist.f"), index_cwd=str(root))
    req = ConnectivityRequest(
        checks=(
            ConnectivityCheck(
                f"{SCOPE_B}.scope_probe",
                f"{TOP}.clk",
                check_id="zz_scope_confident_b",
            ),
        ),
        top=design.top,
        defines={},
    )
    batch, _index, state = run_path_walk_connect(
        req,
        fl,
        top=design.top,
        no_cache=True,
        connect_phase="text",
    )
    row_b = state.rows_by_path.get(SCOPE_B)
    row_a = state.rows_by_path.get(SCOPE_ARM)
    assert row_b is not None, batch.results[0].errors
    assert row_b.module == "zz_scope_B"
    assert row_a is not None
    assert row_a.file.endswith(ZZ_SCOPE_A_RTL)
    assert row_b.file.endswith("zz_scope_b.v")
    assert batch.results[0].connected is True
    assert state.mod_db.defer_count() == 0
    files_a = state.mod_db._module_to_files.get("zz_scope_A", ())
    assert any(str(f).endswith(ZZ_SCOPE_A_RTL) for f in files_a)
    assert any(str(f).endswith(ZZ_SCOPE_A_STUB_RTL) for f in files_a)
    files_b = state.mod_db._module_to_files.get("zz_scope_B", ())
    assert any(str(f).endswith("zz_scope_b.v") for f in files_b)


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