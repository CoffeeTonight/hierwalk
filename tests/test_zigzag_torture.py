"""Zigzag torture corpus: maximum-complexity path-walk + connectivity stress."""

from __future__ import annotations

from pathlib import Path

import pytest

from hierwalk.connect_expand import build_expand_meta, parse_endpoint_elements
from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.connectivity import ConnectivitySession
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex
from hierwalk.connect_artifacts import (
    build_connect_results_from_request,
    collect_hierarchy_evidence,
    format_connect_hierarchy_tsv,
)
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import (
    build_path_walk_state_from_specs,
    create_path_walk_index,
    run_path_walk_connect,
    run_path_walk_index,
)
from hierwalk.zigzag_torture_gen import (
    COLLISION,
    DEEP_ARM,
    DEEP_D2,
    DEEP_D3,
    DEEP_D4,
    DEEP_D5,
    DEEP_DEPTH,
    DESIGN_SUITE_CHECK_ALIASES,
    DW_VENDOR_RTL,
    SHALLOW_ARM,
    SHALLOW_DEPTH,
    SHALLOW_R4,
    TOP,
    ZigzagTortureDesign,
    _suite_conn_checks,
    build_connect_request,
    generate_zigzag_torture_design,
    write_stress_artifacts,
)

ROUND17_CHECK_IDS = (
    "zz_fanin_merge",
    "zz_fanin_merge_decoy",
    "zz_port_expr_xor",
)

ROUND18_EXPAND_CHECK_IDS = (
    "zz_fanin_merge",
    "zz_fanin_merge_decoy",
    "zz_port_expr_xor",
    "zz_expr_mapped",
    "zz_port_concat",
    "zz_port_expr_or",
    "zz_fanin_merge4",
    "zz_loop_range",
    "zz_loop_list",
    "zz_loop_csv",
    "zz_literal_concat",
    "zz_list_endpoints",
)

ROUND18_NEGATIVE_CHECK_IDS = (
    "zz_missing_hierarchy",
    "zz_fanin_merge_decoy",
    "zz_ifdef_inactive",
    "zz_multi_g3_empty",
)

ROUND19_NEW_CHECK_IDS = (
    "zz_gen_tap1",
    "zz_pong_replicate",
    "zz_ff_barrier_tap",
    "zz_multi_g3_empty",
)

ROUND20_NEW_CHECK_IDS = (
    "zz_ifndef_define_mix",
)

ROUND18_NEW_CHECK_IDS = (
    "zz_casex_route",
    "zz_casez_route",
    "zz_ifdef_pass",
    "zz_gen_pass",
    "zz_expr_mapped",
    "zz_zig_to_shallow",
    "zz_zig_decoy",
    "zz_merge_dummy",
    "zz_bb_through",
    "zz_dw_vendor_inst",
    "zz_loop_range",
    "zz_loop_list",
    "zz_loop_csv",
    "zz_port_concat",
    "zz_port_expr_or",
    "zz_fanin_merge4",
    "zz_gen_for_unroll",
    "zz_ifdef_inactive",
    "zz_literal_concat",
    "zz_mid_ifdef_child",
    *ROUND19_NEW_CHECK_IDS,
    *ROUND20_NEW_CHECK_IDS,
)


@pytest.fixture
def torture_bundle(tmp_path: Path) -> tuple[Path, ZigzagTortureDesign]:
    fl, _req, design = write_stress_artifacts(tmp_path / "zz_torture")
    return fl, design


def test_torture_design_shape():
    design = generate_zigzag_torture_design()
    assert design.top == TOP
    assert design.deep_path == DEEP_D5
    assert design.shallow_path == SHALLOW_R4
    assert len(design.checks) == 47
    check_ids = {c.check_id for c in design.checks}
    assert "zz_fanin_merge" in check_ids
    assert "zz_fanin_merge_decoy" in check_ids
    assert "zz_port_expr_xor" in check_ids
    assert "zz_casex_route" in check_ids
    assert "zz_loop_range" in check_ids
    assert "zz_bb_through" in check_ids
    assert "u_bridge_concat" in design.files["zz_deep_d2.v"]
    assert "gen_pass_flat" in design.files["zz_deep_d5.v"]
    assert "gen_tap0" in design.files["zz_deep_d1.v"]
    assert "u_dw_vendor" in design.files["zz_torture_top.v"]
    assert "ff_barrier_tap" in design.files["zz_deep_d1.v"]
    assert "u_empty_multi" in design.files["zz_zigzag.v"]
    assert "gen_tap1" in design.files["zz_deep_d1.v"]
    assert "u_bridge_expr" in design.files["zz_deep_d2.v"]
    assert "u_ifndef_mix" in design.files["zz_deep_d2.v"]
    assert "`ifndef ZZ_IFNDEF_INST_" in design.files["zz_deep_d2.v"]
    assert "`ifndef ZZ_IFNDEF_PING_BODY_" in design.files["zz_common.v"]
    assert "chain_in ^ shallow_return" in design.files["zz_deep_d2.v"]
    assert "assign merge_tap" in design.files["zz_deep_d4.v"]
    assert len(design.files) >= DEEP_DEPTH + SHALLOW_DEPTH + 6
    assert "zz_fake_deep.v" in design.files
    assert DW_VENDOR_RTL in design.files
    assert "zz_torture_top.v" in design.files
    assert "d1_shadow" in design.files["zz_deep_d1.v"]
    assert "u_next_decoy" in design.files["zz_deep_d1.v"]
    assert "r3_alt" in design.files["zz_shallow_r3.v"]
    assert "cube3d" in design.files["zz_deep_d3.v"]
    assert "casex" in design.files["zz_deep_d1.v"]
    assert "casez" in design.files["zz_deep_d3.v"]
    assert "STRB_MAX" in design.files["zz_torture_top.v"]


def test_dw_vendor_inst_design_only_not_in_suite():
    design = generate_zigzag_torture_design()
    assert any(c.check_id == "zz_dw_vendor_inst" for c in design.checks)
    suite_ids = {c["id"] for c in _suite_conn_checks()}
    assert "zz_dw_vendor_inst" not in suite_ids
    assert "zz_dw_vendor_ignored" in suite_ids


def test_round18_design_suite_check_parity():
    """_build_checks() and _suite_conn_checks() must agree on round18 endpoints."""
    design = generate_zigzag_torture_design()
    design_by_id = {c.check_id: c for c in design.checks}
    suite_by_id = {c["id"]: c for c in _suite_conn_checks()}

    for design_id, suite_id in DESIGN_SUITE_CHECK_ALIASES.items():
        dc = design_by_id[design_id]
        sc = suite_by_id[suite_id]
        suite_meta = build_expand_meta(sc["a"], sc["b"], loop=sc.get("loop"))
        if dc.expand is not None:
            assert suite_meta.elements_a == dc.expand.elements_a, design_id
            assert suite_meta.elements_b == dc.expand.elements_b, design_id
        else:
            assert dc.endpoint_a == sc["a"], design_id
            assert dc.endpoint_b == sc["b"], design_id

    for cid in (*ROUND17_CHECK_IDS, *ROUND18_NEW_CHECK_IDS):
        if cid not in suite_by_id:
            continue
        dc = design_by_id[cid]
        sc = suite_by_id[cid]
        raw_a = sc["a"] if isinstance(sc["a"], list) else sc["a"]
        raw_b = sc["b"]
        if dc.expand is not None:
            suite_meta = build_expand_meta(
                raw_a,
                raw_b,
                loop=sc.get("loop"),
            )
            assert suite_meta.map_kind == dc.expand.map_kind, cid
            assert suite_meta.elements_a == dc.expand.elements_a, (cid, "a")
            assert suite_meta.elements_b == dc.expand.elements_b, (cid, "b")
        else:
            assert dc.endpoint_a == sc["a"], cid
            assert dc.endpoint_b == sc["b"], cid

    fanin_a = suite_by_id["zz_fanin_merge"]["a"]
    assert f"{DEEP_D4}.chain_in[1][2]" not in fanin_a


def test_path_walk_index_all_hierarchy_specs(torture_bundle, tmp_path: Path):
    fl_path, design = torture_bundle
    fl = parse_filelist(str(fl_path), index_cwd=str(fl_path.parent))
    index, state, top = run_path_walk_index(
        fl,
        list(design.hierarchy_specs),
        top=design.top,
        extra_defines=build_connect_request(design).defines,
        no_cache=True,
    )
    assert top == TOP
    for spec in design.hierarchy_specs:
        assert spec in state.rows_by_path, spec

    assert state.rows_by_path[DEEP_D5].inst_leaf == "d5"
    assert state.rows_by_path[SHALLOW_R4].inst_leaf == "r4"
    assert state.rows_by_path[DEEP_ARM].inst_leaf == "u_deep"
    assert state.rows_by_path[SHALLOW_ARM].inst_leaf == "u_shallow"
    assert f"{DEEP_ARM}.d1.d1_shadow" in state.rows_by_path
    assert f"{DEEP_ARM}.d1.u_next_decoy" in state.rows_by_path
    assert f"{SHALLOW_ARM}.r1.r2.r3.r3_alt" in state.rows_by_path
    assert index is not None


def test_path_walk_connect_all_checks(torture_bundle, tmp_path: Path):
    """Run each check in isolation for precise failure messages."""
    fl_path, design = torture_bundle
    fl = parse_filelist(str(fl_path), index_cwd=str(fl_path.parent))
    base = build_connect_request(design)
    failures: list[str] = []

    for chk in base.checks:
        req = ConnectivityRequest(
            checks=(chk,),
            top=design.top,
            defines=base.defines,
            include_ff=True,
        )
        batch, _index, _state = run_path_walk_connect(
            req,
            fl,
            top=design.top,
            no_cache=True,
        )
        result = batch.results[0]
        if chk.check_id == "zz_missing_hierarchy":
            if result.connected:
                failures.append(f"{chk.check_id}: expected disconnected")
            elif not any("hierarchy" in e.lower() for e in result.errors):
                failures.append(f"{chk.check_id}: expected hierarchy error")
            continue
        if chk.check_id in ROUND18_NEGATIVE_CHECK_IDS:
            if result.connected:
                failures.append(f"{chk.check_id}: expected disconnected")
            elif chk.check_id == "zz_fanin_merge_decoy" and any(
                "hierarchy" in e.lower() for e in result.errors
            ):
                failures.append(f"{chk.check_id}: expected connectivity miss, not hierarchy")
            continue
        if chk.check_id in ROUND18_EXPAND_CHECK_IDS:
            if not result.sub_results or not all(
                sr.connected for sr in result.sub_results
            ):
                failures.append(f"{chk.check_id}: expand sub-check failed")
            continue
        if chk.check_id == "zz_fanin_merge":
            if not result.sub_results or not all(
                sr.connected for sr in result.sub_results
            ):
                failures.append(f"{chk.check_id}: fan-in merge expand failed")
            continue
        if chk.check_id == "zz_port_expr_xor":
            if not result.sub_results or not all(
                sr.connected for sr in result.sub_results
            ):
                failures.append(f"{chk.check_id}: port-expr xor expand failed")
            continue
        if chk.check_id == "zz_list_endpoints":
            if not result.sub_results or not all(
                sr.connected for sr in result.sub_results
            ):
                failures.append(f"{chk.check_id}: list expand failed")
            continue
        if not result.connected:
            failures.append(
                f"{chk.check_id}: {result.endpoint_a.spec} -> "
                f"{result.endpoint_b.spec} errors={result.errors} note={result.note}"
            )

    if failures:
        pytest.fail("zigzag torture connect failures:\n" + "\n".join(failures[:12]))


def test_path_walk_connect_batch_preserves_check_ids(torture_bundle, tmp_path: Path):
    """Full batch: each result must match its request check_id and endpoints."""
    fl_path, design = torture_bundle
    fl = parse_filelist(str(fl_path), index_cwd=str(fl_path.parent))
    req = build_connect_request(design)
    batch, _index, state = run_path_walk_connect(
        req,
        fl,
        top=design.top,
        no_cache=True,
    )
    assert len(batch.results) == len(design.checks)
    assert state.stats.checks_run == len(design.checks)
    expected = {c.check_id: c for c in req.checks}
    for result, chk in zip(batch.results, req.checks):
        assert result.check_id == chk.check_id
        if chk.check_id in ROUND18_NEGATIVE_CHECK_IDS:
            assert result.connected is False
            continue
        if chk.check_id in ROUND18_EXPAND_CHECK_IDS:
            assert result.sub_results and all(sr.connected for sr in result.sub_results)
            continue
        assert result.endpoint_a.spec == str(chk.endpoint_a)
        assert result.endpoint_b.spec == str(chk.endpoint_b)
        assert result.connected is True, (
            f"{chk.check_id}: errors={result.errors} note={result.note}"
        )


def test_path_walk_logical_parametric_strb_slice(torture_bundle, tmp_path: Path):
    """Logical-conn resolves parametric bus slices on path-walk rows."""
    fl_path, design = torture_bundle
    fl = parse_filelist(str(fl_path), index_cwd=str(fl_path.parent))
    req = ConnectivityRequest(
        checks=(
            ConnectivityCheck(
                f"{TOP}.strb_data[3]",
                f"{DEEP_D3}.strb_in[3]",
                check_id="zz_strb_slice",
            ),
            ConnectivityCheck(
                f"{TOP}.strb_data[1]",
                f"{DEEP_D3}.strb_in[1]",
                check_id="zz_strb_slice1",
            ),
        ),
        top=TOP,
        defines=build_connect_request(design).defines,
        include_ff=True,
    )
    batch, _index, _state = run_path_walk_connect(
        req,
        fl,
        top=TOP,
        no_cache=True,
    )
    by_id = {r.check_id: r for r in batch.results}
    assert by_id["zz_strb_slice"].connected is True
    assert by_id["zz_strb_slice1"].connected is True
    assert by_id["zz_strb_slice"].connected_logical is True


def test_collision_port_prefers_port_not_inst(torture_bundle, tmp_path: Path):
    fl_path, design = torture_bundle
    fl = parse_filelist(str(fl_path), index_cwd=str(fl_path.parent))
    request = ConnectivityRequest(
        checks=(ConnectivityCheck(f"{COLLISION}.w_e", f"{COLLISION}.w_e", "col"),),
        top=TOP,
        defines=build_connect_request(design).defines,
    )
    batch, _index, state = run_path_walk_connect(request, fl, top=TOP, no_cache=True)
    assert batch.results[0].connected is True
    assert f"{COLLISION}.w_e" not in state.rows_by_path


def _full_elab_index(design: ZigzagTortureDesign, rtl_root: Path):
    texts = {
        str((rtl_root / name).resolve()): text
        for name, text in design.files.items()
    }
    index = DesignIndex.build(texts)
    _, rows = elaborate(index, TOP)
    return index, rows


def test_check_connectivity_parametric_strb(torture_bundle, tmp_path: Path):
    fl_path, design = torture_bundle
    index, rows = _full_elab_index(design, fl_path.parent)
    session = ConnectivitySession(
        rows=rows,
        index=index,
        top=TOP,
        resolve_param_dims=True,
    )
    result = session.check(
        f"{TOP}.strb_data[3]",
        f"{DEEP_D3}.strb_in[3]",
    )
    assert result.connected


def test_check_connectivity_text_vs_logical_strb(torture_bundle, tmp_path: Path):
    fl_path, design = torture_bundle
    index, rows = _full_elab_index(design, fl_path.parent)
    text_session = ConnectivitySession(
        rows=rows,
        index=index,
        top=TOP,
        resolve_param_dims=False,
    )
    text_result = text_session.check(f"{TOP}.strb_data", f"{DEEP_ARM}.strb_in")
    assert text_result.connected
    assert not any("STRB_MAX" in err for err in text_result.errors)

    logical_session = ConnectivitySession(
        rows=rows,
        index=index,
        top=TOP,
        resolve_param_dims=True,
    )
    logical_result = logical_session.check(
        f"{TOP}.strb_data[1]",
        f"{DEEP_D3}.strb_in[1]",
    )
    assert logical_result.connected


def test_hierarchy_tsv_list_endpoint_display(torture_bundle, tmp_path: Path):
    fl_path, design = torture_bundle
    fl = parse_filelist(str(fl_path), index_cwd=str(fl_path.parent))
    index, state, _top = run_path_walk_index(
        fl,
        [DEEP_D5, SHALLOW_R4],
        top=design.top,
        extra_defines=build_connect_request(design).defines,
        no_cache=True,
    )
    display_a, _, _, _ = parse_endpoint_elements([DEEP_D5, SHALLOW_R4])
    display_b, _, _, _ = parse_endpoint_elements(f"{DEEP_D5}.leaf_out")
    expand = build_expand_meta([DEEP_D5, SHALLOW_R4], f"{DEEP_D5}.leaf_out")
    req = ConnectivityRequest(
        checks=(
            ConnectivityCheck(
                display_a,
                display_b,
                check_id="hier_lst",
                expand=expand,
            ),
        ),
        top=TOP,
        defines=build_connect_request(design).defines,
    )
    session = ConnectivitySession(
        rows=state.rows(),
        index=index,
        top=TOP,
        resolve_param_dims=True,
    )
    results = build_connect_results_from_request(req, session)
    body = format_connect_hierarchy_tsv(
        results,
        session.rows_by_path,
        phase="text",
        index=index,
        top=TOP,
    )
    assert "[zz" not in body
    assert DEEP_D5 in body
    assert SHALLOW_R4 in body
    evidence = collect_hierarchy_evidence(
        results,
        session.rows_by_path,
        index=index,
        top=TOP,
    )
    paths = {row.path for row in evidence}
    assert "[zz" not in paths
    assert DEEP_D5 in paths
    assert SHALLOW_R4 in paths


def test_decoy_modules_not_on_main_spine(torture_bundle, tmp_path: Path):
    fl_path, design = torture_bundle
    fl = parse_filelist(str(fl_path), index_cwd=str(fl_path.parent))
    index, mod_db = create_path_walk_index(
        fl,
        TOP,
        defines=build_connect_request(design).defines,
        no_cache=True,
    )
    state = build_path_walk_state_from_specs(
        index,
        TOP,
        [DEEP_D5, f"{TOP}.u_zigzag.u_fake_deep"],
        mod_db,
    )
    assert DEEP_D5 in state.rows_by_path
    assert f"{TOP}.u_zigzag.u_fake_deep" not in state.rows_by_path