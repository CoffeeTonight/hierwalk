"""Connect-index memoization: shipped build_module_connect_index + _module_index paths."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hierwalk.cache import set_active_work_dir
from hierwalk.connect_endpoints import (
    _module_index,
    _port_decl_bit_indices,
    _port_decl_md_suffixes,
)
from hierwalk.connect_scan import (
    ModuleConnectIndex,
    _collect_const_assigns_fixed,
    _net_base_in_assign_regex_fast,
    binds_digest,
    build_module_connect_index,
    clear_module_connect_index_cache,
    clear_module_connect_index_mem_cache,
    collect_bind_records_for_module,
    module_connect_index_stats,
    net_representative,
    split_statements,
)
from hierwalk.connectivity import ConnectivitySession
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex, ModuleRecord
from hierwalk.inst_scan import find_hierarchy_instance


def _large_assign_body(n: int = 50_000) -> str:
    assigns = "\n".join(f"assign n{i} = n{i - 1};" for i in range(1, n))
    return (
        "module MOD_A(input a, output z);\n"
        "  wire n0;\n"
        f"  assign z = n{n - 1};\n"
        f"{assigns}\n"
        "  leaf u_tail ( .a(n0) );\n"
        "endmodule\n"
    )


def _design_with_module(body: str, *, file_path: str = "") -> DesignIndex:
    index = DesignIndex({})
    index.modules["MOD_A"] = ModuleRecord(
        module_name="MOD_A",
        file_path=file_path,
        body=body,
        raw_params={},
        instances=[],
    )
    return index


@pytest.fixture(autouse=True)
def _reset_build_index_memo():
    clear_module_connect_index_cache()
    yield
    clear_module_connect_index_cache()


def test_build_module_connect_index_50k_direct_warm_path():
    body = _large_assign_body()
    t0 = time.perf_counter()
    idx_cold = build_module_connect_index(body)
    cold = time.perf_counter() - t0
    uncached_after_cold, hits_after_cold = module_connect_index_stats()
    assert uncached_after_cold == 1
    assert hits_after_cold == 0

    t1 = time.perf_counter()
    idx_warm = build_module_connect_index(body)
    warm = time.perf_counter() - t1
    uncached, hits = module_connect_index_stats()
    assert uncached == 1
    assert hits == 1
    assert idx_warm.rep_adj.keys() == idx_cold.rep_adj.keys()
    assert net_representative(idx_warm, "z") == net_representative(idx_cold, "z")
    assert warm < cold / 10


def test_build_and_module_index_same_50k_body_share_memo():
    body = _large_assign_body()
    index = _design_with_module(body)
    widths = _port_decl_bit_indices(index, "MOD_A", {})
    suffixes = _port_decl_md_suffixes(index, "MOD_A", {})

    idx_direct = build_module_connect_index(
        body,
        port_decl_widths=widths,
        port_decl_md_suffixes=suffixes,
    )
    uncached, hits = module_connect_index_stats()
    assert uncached == 1
    assert hits == 0

    cache: dict = {}
    idx_via_module = _module_index(cache, index, "MOD_A", {}, defines={})
    uncached2, hits2 = module_connect_index_stats()
    assert uncached2 == 1
    assert hits2 == 1
    assert idx_via_module.rep_adj.keys() == idx_direct.rep_adj.keys()


def test_module_index_disk_sidecar_large_body_reentry(tmp_path: Path):
    body = _large_assign_body()
    index = _design_with_module(body)
    work = tmp_path / "work"
    work.mkdir()
    set_active_work_dir(work)
    try:
        cache1: dict = {}
        t0 = time.perf_counter()
        idx1 = _module_index(cache1, index, "MOD_A", {}, defines={})
        cold = time.perf_counter() - t0
        uncached1, _ = module_connect_index_stats()
        assert uncached1 == 1

        clear_module_connect_index_mem_cache()
        cache2: dict = {}
        t1 = time.perf_counter()
        idx2 = _module_index(cache2, index, "MOD_A", {}, defines={})
        disk_hit = time.perf_counter() - t1
        uncached2, _ = module_connect_index_stats()
        sidecars = list((work / "connect_index").glob("*.mci.pkl"))
    finally:
        set_active_work_dir(None)

    assert isinstance(idx1, ModuleConnectIndex)
    assert idx1.rep_adj == idx2.rep_adj
    assert uncached2 == 1
    assert len(sidecars) == 1
    assert disk_hit < cold / 10


def test_module_index_inmem_key_invalidates_on_body_change():
    body_v1 = "module m(input a, output z); assign z = a; endmodule\n"
    body_v2 = "module m(input a, output z); assign z = a; wire extra; endmodule\n"
    index = DesignIndex({})
    index.modules["m"] = ModuleRecord(
        module_name="m",
        file_path="",
        body=body_v1,
        raw_params={},
        instances=[],
    )
    cache: dict = {}
    _module_index(cache, index, "m", {}, defines={})
    rec = index.modules["m"]
    index.modules["m"] = ModuleRecord(
        module_name="m",
        file_path=rec.file_path,
        body=body_v2,
        raw_params=rec.raw_params,
        instances=rec.instances,
    )
    _module_index(cache, index, "m", {}, defines={})
    uncached, _ = module_connect_index_stats()
    assert uncached == 2
    assert len(cache) == 2


def test_connect_kernels_50k_assign_correctness():
    body = _large_assign_body()
    stmts = split_statements(body)
    assert len(stmts) >= 50_000

    consts, _ = _collect_const_assigns_fixed(body)
    assert isinstance(consts, dict)

    assert _net_base_in_assign_regex_fast(body, "missing_net") is False
    assert _net_base_in_assign_regex_fast(body, "z") is True

    edge = find_hierarchy_instance(body, "u_tail")
    assert edge is not None
    assert edge.child_module == "leaf"


_BIND_RTL = """
    module core(input src, output dst);
      assign dst = 1'b0;
    endmodule
    module tie(input src, output dst);
      assign dst = src;
    endmodule
    module top(input src_bind, output out);
      core u_core (.src(1'b0), .dst());
    endmodule
    bind top tie u_tie (.src(src_bind), .dst(u_core.dst));
    """


def _bind_design(tmp_path: Path) -> DesignIndex:
    rtl = tmp_path / "bind.v"
    rtl.write_text(_BIND_RTL, encoding="utf-8")
    return DesignIndex.build({str(rtl): _BIND_RTL})


def test_bind_apply_does_not_pollute_build_memo(tmp_path: Path):
    index = _bind_design(tmp_path)
    body = index.module_body("top")
    widths = _port_decl_bit_indices(index, "top", {})
    suffixes = _port_decl_md_suffixes(index, "top", {})

    clear_module_connect_index_cache()
    pure = build_module_connect_index(
        body,
        port_decl_widths=widths,
        port_decl_md_suffixes=suffixes,
    )
    pure_adj = {k: frozenset(v) for k, v in pure.rep_adj.items()}

    cache: dict = {}
    session = _module_index(cache, index, "top", {}, defines={})

    after = build_module_connect_index(
        body,
        port_decl_widths=widths,
        port_decl_md_suffixes=suffixes,
    )
    _, hits = module_connect_index_stats()
    assert hits >= 1
    after_adj = {k: frozenset(v) for k, v in after.rep_adj.items()}
    assert after_adj == pure_adj

    session_adj = {k: frozenset(v) for k, v in session.rep_adj.items()}
    assert session_adj != pure_adj


def test_module_index_disk_sidecar_bind_module(tmp_path: Path):
    index = _bind_design(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    set_active_work_dir(work)
    try:
        cache1: dict = {}
        idx1 = _module_index(cache1, index, "top", {}, defines={})
        pure_adj = {
            k: frozenset(v)
            for k, v in build_module_connect_index(
                index.module_body("top"),
                port_decl_widths=_port_decl_bit_indices(index, "top", {}),
                port_decl_md_suffixes=_port_decl_md_suffixes(index, "top", {}),
            ).rep_adj.items()
        }
        bound_adj1 = {k: frozenset(v) for k, v in idx1.rep_adj.items()}
        assert bound_adj1 != pure_adj

        clear_module_connect_index_mem_cache()
        cache2: dict = {}
        t0 = time.perf_counter()
        idx2 = _module_index(cache2, index, "top", {}, defines={})
        disk_hit = time.perf_counter() - t0
        bound_adj2 = {k: frozenset(v) for k, v in idx2.rep_adj.items()}
        sidecars = list((work / "connect_index").glob("*.mci.pkl"))
    finally:
        set_active_work_dir(None)

    assert bound_adj1 == bound_adj2
    assert len(sidecars) == 1
    assert disk_hit < 1.0


def test_module_index_disk_sidecar_bind_digest_invalidation(tmp_path: Path):
    rtl_a = tmp_path / "bind_a.v"
    rtl_b = tmp_path / "bind_b.v"
    rtl_a.write_text(_BIND_RTL, encoding="utf-8")
    rtl_b.write_text(
        _BIND_RTL.replace("tie u_tie", "tie u_tie2"),
        encoding="utf-8",
    )
    index_a = DesignIndex.build({str(rtl_a): rtl_a.read_text(encoding="utf-8")})
    index_b = DesignIndex.build({str(rtl_b): rtl_b.read_text(encoding="utf-8")})
    binds_a = collect_bind_records_for_module(index_a, "top")
    binds_b = collect_bind_records_for_module(index_b, "top")
    assert binds_digest(binds_a) != binds_digest(binds_b)

    work = tmp_path / "work"
    work.mkdir()
    set_active_work_dir(work)
    try:
        cache: dict = {}
        idx_a = _module_index(cache, index_a, "top", {}, defines={})
        adj_a = {k: frozenset(v) for k, v in idx_a.rep_adj.items()}

        clear_module_connect_index_mem_cache()
        cache_b: dict = {}
        idx_b = _module_index(cache_b, index_b, "top", {}, defines={})
        adj_b = {k: frozenset(v) for k, v in idx_b.rep_adj.items()}
        sidecars = list((work / "connect_index").glob("*.mci.pkl"))
    finally:
        set_active_work_dir(None)

    assert len(sidecars) == 2
    assert adj_a == adj_b


def test_module_index_session_cache_returns_copy(tmp_path: Path):
    index = _bind_design(tmp_path)
    cache: dict = {}
    idx1 = _module_index(cache, index, "top", {}, defines={})
    idx2 = _module_index(cache, index, "top", {}, defines={})
    assert idx1 is not idx2
    assert idx1.rep_adj == idx2.rep_adj
    idx1.rep_adj.setdefault("_mut_probe", set()).add("x")
    idx3 = _module_index(cache, index, "top", {}, defines={})
    assert "_mut_probe" not in idx3.rep_adj


def test_bind_memo_invalidates_when_bind_file_changes(tmp_path: Path):
    rtl = tmp_path / "design.v"
    rtl.write_text(
        """
        module core(input src, output dst);
          assign dst = 1'b0;
        endmodule
        module top(input src_bind, output out);
          core u_core (.src(1'b0), .dst());
        endmodule
        bind top tie u_tie (.src(src_bind), .dst(u_core.dst));
        module tie(input src, output dst);
          assign dst = src;
        endmodule
        """,
        encoding="utf-8",
    )
    index = DesignIndex.build({str(rtl): rtl.read_text(encoding="utf-8")})
    from hierwalk.connect_endpoints import _resolve_module_index_key

    key1, binds1 = _resolve_module_index_key(
        index, "top", {}, None, ff_barrier=False, over_approximate_if=True
    )
    digest1 = binds_digest(binds1)
    key1_repeat, binds1_repeat = _resolve_module_index_key(
        index, "top", {}, None, ff_barrier=False, over_approximate_if=True
    )
    assert key1_repeat == key1
    assert binds_digest(binds1_repeat) == digest1

    rtl.write_text(
        rtl.read_text(encoding="utf-8").replace("tie u_tie", "tie u_tie2"),
        encoding="utf-8",
    )
    key2, binds2 = _resolve_module_index_key(
        index, "top", {}, None, ff_barrier=False, over_approximate_if=True
    )
    digest2 = binds_digest(binds2)

    assert digest1 != digest2
    assert key1 != key2
    assert binds1[0].inst_leaf != binds2[0].inst_leaf


def test_connectivity_session_reuses_shipped_build_memo(tmp_path: Path):
    v = """
    module top(input logic s0, s1, output logic d0, d1);
      assign d0 = s0;
      assign d1 = s1;
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    session = ConnectivitySession(rows=rows, index=index, top="top")

    r0 = session.check("top.s0", "top.d0")
    r1 = session.check("top.s1", "top.d1")
    uncached, hits = module_connect_index_stats()

    assert r0.connected and r1.connected
    assert uncached == 1
    assert hits == 0