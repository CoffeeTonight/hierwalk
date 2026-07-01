"""Path-walk integration on hc_hierarchy unified_verify corpus."""

from __future__ import annotations

from pathlib import Path

import pytest

from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.cone import fanout_cone
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import (
    build_path_walk_state_from_specs,
    create_path_walk_index,
    run_path_walk_connect,
    run_path_walk_index,
)

_CANDIDATE_ROOTS = (
    Path("/home/user/tools/__CFI/hc_hierarchy/design/unified_verify"),
    Path("/home/user/tools/CodeFromAI/hc_hierarchy/design/unified_verify"),
)

UNIFIED_VERIFY = next((p for p in _CANDIDATE_ROOTS if (p / "filelist.f").is_file()), None)
FILELIST = UNIFIED_VERIFY / "filelist.f" if UNIFIED_VERIFY else None
TOP = "hc_verify_top"


@pytest.mark.skipif(FILELIST is None, reason="unified_verify corpus not available")
def test_unified_verify_anchor_depth_chain_path_walk():
    fl = parse_filelist(str(FILELIST), index_cwd=str(UNIFIED_VERIFY))
    index, mod_db = create_path_walk_index(fl, TOP, defines=dict(fl.defines), no_cache=True)
    paths = [
        f"{TOP}.u_anchor_flat.u_chain.u_d2.u_d3",
        f"{TOP}.u_anchor_flat.u_chain.u_d2.u_d3.u_l",
        f"{TOP}.u_anchor_nested.u_inner.u_chain.u_d2.u_d3",
    ]
    state = build_path_walk_state_from_specs(index, TOP, paths, mod_db)
    for path in paths:
        assert path in state.rows_by_path, path
        row = state.rows_by_path[path]
        assert row.file.endswith("mid_anchor_depth.v")


@pytest.mark.skipif(FILELIST is None, reason="unified_verify corpus not available")
def test_unified_verify_ifdef_instance_under_define():
    fl = parse_filelist(str(FILELIST), index_cwd=str(UNIFIED_VERIFY))
    assert "USE_M1" in fl.defines
    index, mod_db = create_path_walk_index(fl, TOP, defines=dict(fl.defines), no_cache=True)
    state = build_path_walk_state_from_specs(
        index, TOP, [f"{TOP}.u_ifdef.u_system_top"], mod_db,
    )
    assert f"{TOP}.u_ifdef.u_system_top" in state.rows_by_path


@pytest.mark.skipif(FILELIST is None, reason="unified_verify corpus not available")
def test_unified_verify_idx_connect_path_walk():
    fl = parse_filelist(str(FILELIST), index_cwd=str(UNIFIED_VERIFY))
    request = ConnectivityRequest(
        checks=(
            ConnectivityCheck(
                f"{TOP}.idx",
                f"{TOP}.u_ecc_engine_00.idx",
                check_id="idx",
            ),
        ),
        top=TOP,
    )
    batch, _index, state = run_path_walk_connect(
        request, fl, top=TOP, no_cache=True,
    )
    assert batch.results[0].connected is True
    assert f"{TOP}.u_ecc_engine_00" in state.rows_by_path


@pytest.mark.skipif(FILELIST is None, reason="unified_verify corpus not available")
def test_unified_verify_generate_fanout_cone_path_walk():
    """if-generate instance (gen_on.u_on) needs filelist +define+ENABLE for cone fold."""
    fl = parse_filelist(str(FILELIST), index_cwd=str(UNIFIED_VERIFY))
    endpoint = f"{TOP}.u_gen_if.gen_on.u_on.done"
    index, state, top = run_path_walk_index(
        fl,
        [endpoint],
        top=TOP,
        no_cache=True,
    )
    result = fanout_cone(
        endpoint,
        rows=state.rows(),
        index=index,
        top=top,
        defines=dict(fl.defines),
    )
    assert not result.errors
    scopes = [b.scope for b in result.boundaries]
    assert any("gen_on.u_on" in s for s in scopes), scopes


@pytest.mark.skipif(FILELIST is None, reason="unified_verify corpus not available")
def test_unified_verify_md2d_deep_path_walk():
    """Multi-dim arrays: two branches cross-linked via probe_out/probe_in."""
    fl = parse_filelist(str(FILELIST), index_cwd=str(UNIFIED_VERIFY))
    deep_a = f"{TOP}.u_md2d.a.b.c[0][1].d.e.f[1].g[0][2]"
    deep_b = f"{TOP}.u_md2d.a2.b.c[1][0].d.e.f[0].g[1][1]"
    index, state, top = run_path_walk_index(
        fl,
        [deep_a, deep_b, f"{deep_a}.clk", f"{TOP}.clk"],
        top=TOP,
        no_cache=True,
    )
    assert deep_a in state.rows_by_path
    assert deep_b in state.rows_by_path
    assert state.rows_by_path[deep_a].inst_leaf == "g[0][2]"
    assert state.rows_by_path[deep_b].inst_leaf == "g[1][1]"
    f1 = f"{TOP}.u_md2d.a.b.c[0][1].d.e.f[1]"
    request = ConnectivityRequest(
        checks=(
            ConnectivityCheck(f"{TOP}.clk", f"{deep_a}.clk", check_id="md2d_clk"),
            ConnectivityCheck(
                f"{deep_a}.probe_out",
                f"{deep_b}.probe_in",
                check_id="md2d_branch_link",
            ),
            ConnectivityCheck(
                f"{f1}.leaf_out[0][2]",
                f"{deep_a}.probe_out",
                check_id="md2d_wire_leaf",
            ),
            ConnectivityCheck(
                f"{TOP}.md2d_probe_sink",
                f"{TOP}.status[0]",
                check_id="md2d_wire_top",
            ),
        ),
        top=TOP,
    )
    batch, _idx2, _st2 = run_path_walk_connect(request, fl, top=TOP, no_cache=True)
    by_id = {r.check_id: r for r in batch.results}
    assert by_id["md2d_clk"].connected is True
    assert by_id["md2d_branch_link"].connected is True
    assert by_id["md2d_wire_leaf"].connected is True
    assert by_id["md2d_wire_top"].connected is True


@pytest.mark.skipif(FILELIST is None, reason="unified_verify corpus not available")
def test_unified_verify_zigzag_bus_path_walk():
    """Zigzag depth + a[2:0][3:0] bus: deep arm, shallow arm, FF/comb io_trace."""
    fl = parse_filelist(str(FILELIST), index_cwd=str(UNIFIED_VERIFY))
    deep_d5 = f"{TOP}.u_zigzag.u_deep.d1.d2.d3.d4.d5"
    shallow_r4 = f"{TOP}.u_zigzag.u_shallow.r1.r2.r3.r4"
    index, state, top = run_path_walk_index(
        fl,
        [deep_d5, shallow_r4, f"{TOP}.u_zigzag.u_deep", f"{TOP}.u_zigzag.u_shallow"],
        top=TOP,
        no_cache=True,
    )
    assert deep_d5 in state.rows_by_path
    assert shallow_r4 in state.rows_by_path
    assert state.rows_by_path[deep_d5].inst_leaf == "d5"
    assert state.rows_by_path[shallow_r4].inst_leaf == "r4"
    request = ConnectivityRequest(
        checks=(
            ConnectivityCheck(
                f"{TOP}.data[0]",
                f"{TOP}.u_zigzag.u_deep.a[0][0]",
                check_id="zz_src_to_deep_a00",
            ),
            ConnectivityCheck(
                f"{TOP}.u_zigzag.u_deep.mid_tap",
                f"{TOP}.u_zigzag.u_shallow.a",
                check_id="zz_deep_to_shallow",
            ),
            ConnectivityCheck(
                f"{TOP}.u_zigzag.u_deep.mid_tap[1][2]",
                f"{TOP}.u_zigzag.u_shallow.a[1][2]",
                check_id="zz_deep_to_shallow_slice",
            ),
            ConnectivityCheck(
                f"{TOP}.clk",
                f"{deep_d5}.clk",
                check_id="zz_clk_deep",
            ),
        ),
        top=TOP,
    )
    batch, _idx2, _st2 = run_path_walk_connect(request, fl, top=TOP, no_cache=True)
    by_id = {r.check_id: r for r in batch.results}
    assert by_id["zz_src_to_deep_a00"].connected is True
    assert by_id["zz_deep_to_shallow"].connected is True
    assert by_id["zz_deep_to_shallow_slice"].connected is True
    assert by_id["zz_clk_deep"].connected is True
    from hierwalk.inst_trace import InstTraceRequest, run_inst_trace

    io = run_inst_trace(
        InstTraceRequest(
            instance=deep_d5,
            direction="driver",
            path_kind="comb",
        ),
        rows=state.rows(),
        index=index,
        top=top,
        defines=dict(fl.defines),
    )
    assert not io.errors
    assert any(pr.port_name == "a[2:0][3:0]" for pr in io.port_results)
    assert io.boundaries