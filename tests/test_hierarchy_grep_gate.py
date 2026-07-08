"""Tier0 hierarchy_grep gate for text-conn."""

from __future__ import annotations

from pathlib import Path

import pytest

from hierwalk.connect.hierarchy_grep_gate import (
    flat_rows_from_resolve,
    gate_connect_check,
    prepare_hierarchy_grep_session,
)
from hierwalk.connect.shared.request import ConnectivityCheck
from hierwalk.hierarchy_grep import resolve_hierarchy_grep
from hierwalk.index import DesignIndex


def _write(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p.resolve())


def test_gate_strips_port_tail_for_hierarchy_resolve(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (output logic probe);
          assign probe = 1'b0;
        endmodule
        module top (input logic clk);
          child u_a ();
        endmodule
        """,
    )
    session = prepare_hierarchy_grep_session([top_v], top="top")
    session.file_grep_index(wait=True)
    chk = ConnectivityCheck("top.u_a.probe", "top.clk", check_id="t0")
    gate = gate_connect_check(chk, session, top="top", index=DesignIndex({}))
    ep_a = gate.endpoint_gates[0]
    assert ep_a.hierarchy == "top.u_a"
    assert ep_a.port_tail == "probe"
    assert ep_a.ok


def test_gate_pass_builds_rows_and_scoped_files(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (output logic out);
          assign out = 1'b0;
        endmodule
        module top;
          child u_a ();
        endmodule
        """,
    )
    session = prepare_hierarchy_grep_session([top_v], top="top")
    session.file_grep_index(wait=True)
    index = DesignIndex({})
    chk = ConnectivityCheck("top.u_a.out", "top.u_a.out", check_id="t1")
    gate = gate_connect_check(chk, session, top="top", index=index)
    assert gate.status == "pass"
    assert gate.use_grep_fast_path
    assert len(gate.scoped_files) >= 1
    assert any(r.full_path == "top.u_a" for r in gate.rows)


def test_scoped_sources_implicated_only_not_whole_filelist(tmp_path: Path):
    from hierwalk.index import DesignIndex

    top_v = _write(
        tmp_path,
        "top.v",
        """
        module leaf (); endmodule
        module top;
          leaf u_a ();
        endmodule
        """,
    )
    child_v = _write(tmp_path, "child.v", "module child (); endmodule\n")
    fl_path = tmp_path / "fl.f"
    fl_path.write_text(f"{top_v}\n{child_v}\n", encoding="utf-8")
    index = DesignIndex.build(
        {top_v: Path(top_v).read_text(encoding="utf-8"), child_v: Path(child_v).read_text(encoding="utf-8")},
        file_via_filelist={top_v: str(fl_path), child_v: str(fl_path)},
        file_filelist_chain={top_v: str(fl_path), child_v: str(fl_path)},
    )
    session = prepare_hierarchy_grep_session([top_v, child_v], top="top")
    session.file_grep_index(wait=True)
    chk = ConnectivityCheck("top.u_a", "top.u_a", check_id="t3")
    gate = gate_connect_check(chk, session, top="top", index=index)
    from hierwalk.connect.hierarchy_grep_gate import scoped_sources_for_gate

    scoped = scoped_sources_for_gate(gate, [top_v, child_v], index=index)
    assert top_v in scoped
    assert child_v not in scoped


def test_zz_gen_tap1_gate_passes_wire_tail_not_inst(tmp_path: Path):
    """Grep may label a wire as inst; gate must recognize module-local signal tails."""
    from hierwalk.zigzag_torture_gen import build_connect_request, write_stress_artifacts

    fl, _req, design = write_stress_artifacts(tmp_path / "zz")
    sources = [str(p) for p in fl.parent.glob("*.v")]
    index = DesignIndex.build(
        {str(p): Path(p).read_text(encoding="utf-8") for p in fl.parent.glob("*.v")}
    )
    session = prepare_hierarchy_grep_session(sources, top=design.top)
    session.file_grep_index(wait=True)
    base = build_connect_request(design)
    chk = next(c for c in base.checks if c.check_id == "zz_gen_tap1")
    gate = gate_connect_check(chk, session, top=design.top, index=index)
    assert gate.status == "pass"
    assert gate.use_grep_fast_path


def test_gate_miss_rejects_without_fallback(tmp_path: Path):
    top_v = _write(tmp_path, "top.v", "module top; endmodule\n")
    session = prepare_hierarchy_grep_session([top_v], top="top")
    chk = ConnectivityCheck("top.u_missing.sig", "top.clk", check_id="t2")
    gate = gate_connect_check(chk, session, top="top", index=DesignIndex({}))
    assert gate.status == "reject"
    assert gate.fast_fail_result is not None
    assert not gate.fast_fail_result.connected


def test_connect_both_phase_emits_hgrep_gate_log(tmp_path: Path):
    """Default connect_phase=both must still tier0-gate text-conn."""
    from hierwalk.connect.shared.request import ConnectivityRequest
    from hierwalk.filelist import parse_filelist
    from hierwalk.path_walk import run_path_walk_connect

    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (output logic out);
          assign out = 1'b0;
        endmodule
        module top (input logic clk);
          child u_a ();
        endmodule
        """,
    )
    fl = tmp_path / "fl.f"
    fl.write_text(f"{top_v}\n", encoding="utf-8")
    fl_result = parse_filelist(str(fl))
    log_path = tmp_path / "walk.log"
    request = ConnectivityRequest(
        checks=(ConnectivityCheck("top.u_a.out", "top.u_a.out", check_id="hg1"),),
        top="top",
    )
    batch, _index, _state = run_path_walk_connect(
        request,
        fl_result,
        top="top",
        no_cache=True,
        trace_log_path=log_path,
        connect_phase="both",
    )
    assert batch.results[0].connected, batch.results[0].errors
    log_text = log_path.read_text(encoding="utf-8")
    assert "hgrep-gate check=hg1" in log_text
    assert "status=pass" in log_text


def test_gate_pass_skips_full_hierarchy_walk(monkeypatch, tmp_path: Path):
    """Gate pass uses inst-chain seed only; full per-check walk is for fallback."""
    from hierwalk.connect.shared.request import ConnectivityRequest
    from hierwalk.filelist import parse_filelist
    from hierwalk.path_walk import run_path_walk_connect

    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (output logic out);
          assign out = 1'b0;
        endmodule
        module top (input logic clk);
          child u_a ();
        endmodule
        """,
    )
    fl = tmp_path / "fl.f"
    fl.write_text(f"{top_v}\n", encoding="utf-8")
    fl_result = parse_filelist(str(fl))

    def _forbidden_walk(*_args, **_kwargs):
        raise AssertionError("_walk_hierarchy_for_check must not run on hgrep gate pass")

    def _forbidden_ensure_path(self, *_args, **_kwargs):
        raise AssertionError("ensure_path must not run on hgrep gate pass")

    import hierwalk.path_walk as pw

    monkeypatch.setattr(pw, "_walk_hierarchy_for_check", _forbidden_walk)
    monkeypatch.setattr(pw.PathWalkState, "ensure_path", _forbidden_ensure_path)

    log_path = tmp_path / "walk.log"
    request = ConnectivityRequest(
        checks=(ConnectivityCheck("top.u_a.out", "top.u_a.out", check_id="skip-walk"),),
        top="top",
    )
    batch, _index, _state = run_path_walk_connect(
        request,
        fl_result,
        top="top",
        no_cache=True,
        connect_phase="text",
        trace_log_path=log_path,
    )
    assert batch.results[0].connected, batch.results[0].errors
    log_text = log_path.read_text(encoding="utf-8")
    assert "hgrep-gate check=skip-walk" in log_text
    assert "status=pass" in log_text
    assert "connect-pipeline hgrep-fast" in log_text
    assert "connect-pipeline hierarchy-ready" not in log_text


def test_fold_gate_rows_sets_param_ctx(tmp_path: Path):
    from hierwalk.connect.hierarchy_grep_gate import fold_gate_rows_with_param_ctx
    from hierwalk.index import DesignIndex

    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (output logic out);
          assign out = 1'b0;
        endmodule
        module top;
          child u_a ();
        endmodule
        """,
    )
    index = DesignIndex.build({top_v: Path(top_v).read_text(encoding="utf-8")})
    session = prepare_hierarchy_grep_session([top_v], top="top")
    session.file_grep_index(wait=True)
    result = resolve_hierarchy_grep("top.u_a", top="top", rtl_paths=[top_v])
    rows = flat_rows_from_resolve(result, index=index)
    folded = fold_gate_rows_with_param_ctx(rows, index=index, top="top")
    by_path = {r.full_path: r for r in folded}
    assert by_path["top"].param_ctx_folded
    assert by_path["top.u_a"].param_ctx_folded


def test_flat_rows_from_resolve_inst_chain(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module leaf (); endmodule
        module top;
          leaf u_b ();
        endmodule
        """,
    )
    result = resolve_hierarchy_grep("top.u_b", top="top", rtl_paths=[top_v])
    rows = flat_rows_from_resolve(result, index=DesignIndex({}))
    paths = {r.full_path for r in rows}
    assert "top" in paths
    assert "top.u_b" in paths