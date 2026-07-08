"""Zigzag torture: grep-first hierarchy resolve on complex paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from hierwalk.filelist import parse_filelist
from hierwalk.hierarchy_grep import build_module_index, resolve_hierarchy_grep
from hierwalk.zigzag_annex_gen import matrix_hierarchy_specs
from hierwalk.zigzag_torture_gen import (
    DEEP_D5,
    DEEP_D2,
    D1_SHADOW,
    D1_NEXT_DECOY,
    R3_ALT,
    SCOPE_B,
    SHALLOW_R4,
    TOP,
    ZZ_SCOPE_A_RTL,
    ZZ_SCOPE_B_RTL,
    ZZ_SCOPE_B_STUB_RTL,
    ZZ_SCOPE_DECOY_RTL,
    write_stress_artifacts,
)


@pytest.fixture(scope="module")
def zigzag_grep_bundle(tmp_path_factory) -> tuple[list[str], object]:
    root = tmp_path_factory.mktemp("zz_hgrep_zigzag")
    fl_path, _req, design = write_stress_artifacts(root)
    fl = parse_filelist(str(fl_path), index_cwd=str(fl_path.parent))
    rtl = [str(p.resolve()) for p in fl.source_files]
    return rtl, design


def test_zigzag_deepest_chain(zigzag_grep_bundle):
    rtl, _design = zigzag_grep_bundle
    index = build_module_index(rtl)
    result = resolve_hierarchy_grep(DEEP_D5, top=TOP, rtl_paths=rtl, module_index=index)
    assert result["ok"], result.get("error")
    files = [Path(n["file"]).name for n in result["nodes"] if n.get("file")]
    assert files[-1] == "zz_deep_d4.v"
    assert result["nodes"][-1]["child_module"] == "zz_deep_d5"


def test_zigzag_deepest_leaf_signal(zigzag_grep_bundle):
    rtl, _design = zigzag_grep_bundle
    index = build_module_index(rtl)
    result = resolve_hierarchy_grep(
        f"{DEEP_D5}.leaf_out",
        top=TOP,
        rtl_paths=rtl,
        module_index=index,
    )
    assert result["ok"], result.get("error")
    assert result["nodes"][-1]["kind"] == "signal"
    assert Path(result["nodes"][-1]["file"]).name == "zz_deep_d5.v"


def test_zigzag_scope_wire_inst_collision(zigzag_grep_bundle):
    rtl, _design = zigzag_grep_bundle
    index = build_module_index(rtl)
    result = resolve_hierarchy_grep(SCOPE_B, top=TOP, rtl_paths=rtl, module_index=index)
    assert result["ok"], result.get("error")
    assert result["nodes"][-1]["kind"] == "inst"
    assert result["nodes"][-1]["child_module"] == "zz_scope_B"


def test_zigzag_all_hierarchy_specs(zigzag_grep_bundle):
    rtl, design = zigzag_grep_bundle
    index = build_module_index(rtl)
    failures: list[str] = []
    for spec in design.hierarchy_specs:
        result = resolve_hierarchy_grep(spec, top=TOP, rtl_paths=rtl, module_index=index)
        if not result["ok"]:
            failures.append(f"{spec}: {result.get('error', '')}")
    assert not failures, "hierarchy_grep misses:\n" + "\n".join(failures)


def test_zigzag_matrix_nested_generate(zigzag_grep_bundle):
    rtl, _design = zigzag_grep_bundle
    index = build_module_index(rtl)
    path = "zz_torture_top.u_matrix.outer[0].inner[0].u_nest"
    result = resolve_hierarchy_grep(path, top=TOP, rtl_paths=rtl, module_index=index)
    assert result["ok"], result.get("error")
    assert result["hierarchy"] == "zz_torture_top.u_matrix.outer.inner.u_nest"
    assert result["nodes"][-1]["child_module"] == "NEST"


def test_zigzag_mid_tap_slice_resolves_to_base_port(zigzag_grep_bundle):
    rtl, _design = zigzag_grep_bundle
    index = build_module_index(rtl)
    from hierwalk.zigzag_torture_gen import CONE_FANOUT

    result = resolve_hierarchy_grep(CONE_FANOUT, top=TOP, rtl_paths=rtl, module_index=index)
    assert result["ok"], result.get("error")
    assert result["hierarchy"].endswith(".mid_tap")
    assert result["nodes"][-1]["kind"] == "port"


def test_zigzag_worried_cases_single_branch(zigzag_grep_bundle):
    """Dup-module / wire-inst collision paths must prune stubs and decoys."""
    rtl, _design = zigzag_grep_bundle
    index = build_module_index(rtl)

    scope_b = resolve_hierarchy_grep(SCOPE_B, top=TOP, rtl_paths=rtl, module_index=index)
    assert scope_b["ok"], scope_b.get("error")
    assert not scope_b["ambiguous"], scope_b.get("candidates")
    u_b = scope_b["nodes"][-1]
    assert u_b["kind"] == "inst"
    assert u_b["child_module"] == "zz_scope_B"
    assert Path(u_b["hit_file"]).name == ZZ_SCOPE_A_RTL
    assert Path(u_b["child_decl_file"]).name == ZZ_SCOPE_B_RTL
    assert ZZ_SCOPE_B_STUB_RTL not in Path(u_b["child_decl_file"]).name

    scope_probe = resolve_hierarchy_grep(
        f"{SCOPE_B}.scope_probe",
        top=TOP,
        rtl_paths=rtl,
        module_index=index,
    )
    assert scope_probe["ok"], scope_probe.get("error")
    assert not scope_probe["ambiguous"], scope_probe.get("candidates")
    leaf = scope_probe["nodes"][-1]
    assert leaf["kind"] == "port"
    assert Path(leaf["hit_file"]).name == ZZ_SCOPE_B_RTL
    assert ZZ_SCOPE_B_STUB_RTL not in Path(leaf["hit_file"]).name

    d1_shadow = resolve_hierarchy_grep(D1_SHADOW, top=TOP, rtl_paths=rtl, module_index=index)
    assert d1_shadow["ok"], d1_shadow.get("error")
    assert not d1_shadow["ambiguous"]
    leaf_shadow = d1_shadow["nodes"][-1]
    assert leaf_shadow["child_module"] == "zz_decoy"
    assert Path(leaf_shadow["hit_file"]).name == "zz_deep_d1.v"

    deep = resolve_hierarchy_grep(DEEP_D5, top=TOP, rtl_paths=rtl, module_index=index)
    assert deep["ok"]
    assert not deep["ambiguous"]

    # zz_scope_A dup index must include decoy + stub, but u_a hop prunes them.
    assert len(index["zz_scope_A"]) >= 3
    assert any(ZZ_SCOPE_DECOY_RTL in p for p in index["zz_scope_A"])
    assert any(ZZ_SCOPE_B_STUB_RTL in p for p in index["zz_scope_B"])
    scope_a_hop = next(n for n in scope_b["nodes"] if n["segment"] == "u_a")
    assert Path(scope_a_hop["hit_file"]).name == "zz_torture_top.v"
    assert Path(scope_a_hop["child_decl_file"]).name == ZZ_SCOPE_A_RTL
    assert ZZ_SCOPE_DECOY_RTL not in Path(scope_a_hop["child_decl_file"]).name


def test_zigzag_complex_probes_logged(zigzag_grep_bundle, capsys):
    """Emit probe log for manual inspection (pytest -s)."""
    rtl, design = zigzag_grep_bundle
    index = build_module_index(rtl)
    probes = [
        DEEP_D5,
        f"{DEEP_D5}.leaf_out",
        SHALLOW_R4,
        f"{DEEP_D2}.u_ifndef_mix",
        D1_SHADOW,
        D1_NEXT_DECOY,
        SCOPE_B,
        R3_ALT,
        *matrix_hierarchy_specs(torture_top=TOP)[:3],
    ]
    for hier in probes:
        result = resolve_hierarchy_grep(hier, top=TOP, rtl_paths=rtl, module_index=index)
        chain = " -> ".join(
            f"{n['segment']}@{Path(n['file']).name}"
            f"({n.get('elapsed_ms', '?')}ms)"
            for n in result["nodes"][1:]
        )
        print(
            f"[{result.get('resolved_at', '')}] "
            f"[{'OK' if result['ok'] else 'FAIL'}] "
            f"{hier} total={result.get('total_elapsed_ms', '?')}ms :: {chain}"
        )
    assert resolve_hierarchy_grep(DEEP_D5, top=TOP, rtl_paths=rtl, module_index=index)["ok"]