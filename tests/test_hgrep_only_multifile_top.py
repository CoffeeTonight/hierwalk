"""Pure hgrep: multi-file top root branches + hgrep_only gate semantics."""

from __future__ import annotations

from pathlib import Path

from hierwalk.connect.hierarchy_grep_gate import (
    gate_connect_check,
    prepare_hierarchy_grep_session,
    run_hgrep_connect_batch,
)
from hierwalk.connect.shared.request import (
    ConnectivityCheck,
    parse_connect_request_json,
)
from hierwalk.hierarchy_grep import resolve_hierarchy_grep
from hierwalk.index import DesignIndex


def _write(tmp: Path, name: str, text: str) -> str:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return str(p.resolve())


def test_resolve_multi_file_top_uses_real_decl_not_empty_stub(tmp_path: Path):
    """Root must not hardcode index[top][0] when a later file holds real body."""
    stub = _write(
        tmp_path,
        "top_stub.v",
        """
        module top;
        endmodule
        """,
    )
    real = _write(
        tmp_path,
        "top_real.v",
        """
        module child (output logic o);
          assign o = 1'b1;
        endmodule
        module top;
          child u0 (.o());
        endmodule
        """,
    )
    # Order: stub first so old code would pick empty top_stub.v
    index = {
        "top": [stub, real],
        "child": [real],
    }
    res = resolve_hierarchy_grep(
        "top.u0.o",
        top="top",
        module_index=index,
        rtl_paths=[stub, real],
    )
    assert res["ok"], res.get("error")
    # Should resolve via real file, not fail on empty stub-only root.
    files = {n.get("file") or n.get("hit_file") for n in res.get("nodes") or []}
    assert real in files or any(
        (n.get("child_decl_file") == real) for n in (res.get("nodes") or [])
    )


def test_hgrep_only_ambiguous_is_pass_not_fallback(tmp_path: Path):
    """Multi-file ok resolve: pregate fallbacks, pure hgrep passes."""
    # Two non-empty decls for same module → resolve can be multi-branch.
    a = _write(
        tmp_path,
        "a.v",
        """
        module leaf (output logic o);
          assign o = 1'b0;
        endmodule
        module top;
          leaf u0 (.o());
        endmodule
        """,
    )
    b = _write(
        tmp_path,
        "b.v",
        """
        module leaf (output logic o);
          assign o = 1'b1;
        endmodule
        module top;
          leaf u0 (.o());
        endmodule
        """,
    )
    session = prepare_hierarchy_grep_session([a, b], top="top")
    session.file_grep_index(wait=True)
    chk = ConnectivityCheck("top.u0.o", "top.u0.o", check_id="amb")

    pregate = gate_connect_check(
        chk, session, top="top", index=DesignIndex({}), hgrep_only=False
    )
    pure = gate_connect_check(
        chk, session, top="top", index=DesignIndex({}), hgrep_only=True
    )
    # If resolve is non-ambiguous (prune leaves one), both pass — still OK.
    if pure.status == "pass":
        assert pure.use_grep_fast_path
    # When ambiguous, pure must not fall back for existence-only.
    if any(g.ambiguous for g in pure.endpoint_gates or ()):
        assert pure.status == "pass", pure.log_line
        assert pregate.status == "fallback", pregate.log_line


def test_hgrep_only_skips_inst_coverage_false_fallback(tmp_path: Path):
    """Empty DesignIndex must not force inst-coverage fallback in pure hgrep."""
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module leaf ();
        endmodule
        module top;
          leaf u_a ();
        endmodule
        """,
    )
    session = prepare_hierarchy_grep_session([top_v], top="top")
    session.file_grep_index(wait=True)
    # inst-only endpoint (no port) exercises _inst_endpoints_need_walk
    chk = ConnectivityCheck("top.u_a", "top.u_a", check_id="inst")
    empty = DesignIndex({})
    pregate = gate_connect_check(
        chk, session, top="top", index=empty, hgrep_only=False
    )
    pure = gate_connect_check(
        chk, session, top="top", index=empty, hgrep_only=True
    )
    assert pure.status == "pass", pure.log_line
    # pregate may fallback on inst-coverage with empty index
    if pregate.status == "fallback":
        assert "inst-coverage" in pregate.log_line or "ambiguous" in pregate.log_line


def test_run_hgrep_batch_passes_hgrep_only(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module leaf (output logic o);
          assign o = 1'b0;
        endmodule
        module top;
          leaf u_a (.o());
        endmodule
        """,
    )
    req = parse_connect_request_json(
        {
            "top": "top",
            "checks": [{"id": "c", "a": "top.u_a.o", "b": "top.u_a.o"}],
        }
    )
    batch, _idx, rows = run_hgrep_connect_batch(
        req,
        [top_v],
        top="top",
        connect_output_dir=tmp_path / "db",
    )
    assert len(batch.results) == 1
    assert batch.results[0].connected
    assert batch.results[0].mode == "hgrep"
