"""Tier0 hierarchy_grep gate for text-conn."""

from __future__ import annotations

from pathlib import Path

import pytest

from hierwalk.connect.hierarchy_grep_gate import (
    _emit_hgrep_check_milestones,
    emit_hgrep_gate_log,
    flat_rows_from_resolve,
    format_hierarchy_grep_gate_report,
    gate_connect_check,
    prepare_hierarchy_grep_session,
)
from hierwalk.connect.shared.request import ConnectivityCheck
from hierwalk.hierarchy_grep import GREP_HIE_JSON_NAME, resolve_hierarchy_grep
from hierwalk.index import DesignIndex


def _write(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p.resolve())


def test_emit_hgrep_gate_log_includes_timestamp(capsys):
    emit_hgrep_gate_log("hgrep-cache hit path=/tmp/grep_hie.json")
    err = capsys.readouterr().err
    assert err.startswith("20")
    assert "[hier-walk path-walk] hgrep-cache hit" in err


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


def test_gate_connect_check_writes_report_on_finish(tmp_path: Path):
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
    report_path = tmp_path / "conn.hgrep_gate.report"
    chk = ConnectivityCheck("top.u_a.out", "top.u_a.out", check_id="rep1")
    gate = gate_connect_check(
        chk,
        session,
        top="top",
        index=DesignIndex({}),
        report_path=report_path,
    )
    assert gate.status == "pass"
    text = report_path.read_text(encoding="utf-8")
    assert "Hierarchy grep gate batch report" in text
    assert "--- check rep1 ---" in text
    assert '"status": "pass"' in text
    assert "hgrep-gate-report" not in text  # file only; stderr line checked separately


def test_format_hierarchy_grep_gate_report_contains_json(tmp_path: Path):
    top_v = _write(tmp_path, "top.v", "module top; endmodule\n")
    session = prepare_hierarchy_grep_session([top_v], top="top")
    chk = ConnectivityCheck("top.x", "top.x", check_id="fmt1")
    gate = gate_connect_check(chk, session, top="top", index=DesignIndex({}))
    report = format_hierarchy_grep_gate_report(
        gate,
        check_id="fmt1",
        endpoint_a=chk.endpoint_a,
        endpoint_b=chk.endpoint_b,
        top="top",
    )
    assert "json:" in report
    assert '"check_id": "fmt1"' in report


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


def test_connect_phase_hgrep_only_skips_connect_coi(tmp_path: Path):
    """JSON connect_phase=hgrep runs gate only; no connect-coi in log."""
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
    out_dir = tmp_path / "out"
    log_path = tmp_path / "walk.log"
    request = ConnectivityRequest(
        checks=(ConnectivityCheck("top.u_a.out", "top.u_a.out", check_id="hg_only"),),
        top="top",
    )
    batch, _index, _state = run_path_walk_connect(
        request,
        fl_result,
        top="top",
        no_cache=True,
        connect_phase="hgrep",
        connect_output_dir=out_dir,
        trace_log_path=log_path,
    )
    assert batch.results[0].connected
    assert batch.results[0].mode == "hgrep"
    report = out_dir / "conn.hgrep_gate.report"
    assert report.is_file()
    log_text = log_path.read_text(encoding="utf-8")
    assert "connect-coi begin" not in log_text
    assert "connect-coi done" not in log_text
    assert "hgrep-gate check=hg_only" in log_text
    assert "connect-hgrep done" in log_text


def test_connect_pipeline_hgrep_before_connect_coi(tmp_path: Path):
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
        checks=(ConnectivityCheck("top.u_a.out", "top.u_a.out", check_id="ord1"),),
        top="top",
    )
    run_path_walk_connect(
        request,
        fl_result,
        top="top",
        no_cache=True,
        connect_phase="text",
        trace_log_path=log_path,
        connect_output_dir=tmp_path / "out",
    )
    log_text = log_path.read_text(encoding="utf-8")
    gate_pos = log_text.find("hgrep-gate check=ord1")
    report_pos = log_text.find("connect-pipeline hgrep-gate-report path=")
    coi_pos = log_text.find("connect-coi begin")
    assert gate_pos >= 0, log_text
    assert report_pos >= 0, log_text
    assert coi_pos >= 0, log_text
    assert gate_pos < coi_pos, "hgrep-gate must precede connect-coi begin"
    assert report_pos < coi_pos, "report path must precede connect-coi begin"


def test_connect_pipeline_writes_hgrep_gate_report(tmp_path: Path):
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
    out_dir = tmp_path / "out"
    request = ConnectivityRequest(
        checks=(ConnectivityCheck("top.u_a.out", "top.u_a.out", check_id="hg1"),),
        top="top",
    )
    batch, _index, _state = run_path_walk_connect(
        request,
        fl_result,
        top="top",
        no_cache=True,
        connect_phase="text",
        connect_output_dir=out_dir,
    )
    assert batch.results[0].connected, batch.results[0].errors
    report_path = out_dir / "conn.hgrep_gate.report"
    assert report_path.is_file(), f"missing gate report under {out_dir}"
    text = report_path.read_text(encoding="utf-8")
    assert "--- check hg1 ---" in text
    assert '"status": "pass"' in text


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


def test_prepare_hierarchy_grep_session_writes_grep_hie_json(tmp_path: Path):
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
    work = tmp_path / "work"
    logs: list[str] = []

    session = prepare_hierarchy_grep_session(
        [top_v],
        top="top",
        work_dir=work,
        on_emit=logs.append,
    )
    cache = work / GREP_HIE_JSON_NAME
    assert cache.is_file()
    assert any("hgrep-cache write" in line for line in logs)
    assert session.resolve("top.u_a.out", top="top")["ok"] is True
    milestone_lines = [line for line in logs if "hgrep-hie milestone" in line]
    assert any("filelist-ready sources=1 top=top" in line for line in milestone_lines)
    assert any("rtl-db-build-start rtl_files=1" in line for line in milestone_lines)
    assert any("rtl-db-built modules=2 rtl_files=1" in line for line in milestone_lines)
    assert any("grep-hie-index-ready" in line for line in milestone_lines)
    assert any("grep-hie-saved" in line for line in milestone_lines)


def test_prepare_hierarchy_grep_session_reuses_grep_hie_cache(tmp_path: Path):
    top_v = _write(tmp_path, "top.v", "module top; wire x; endmodule\n")
    work = tmp_path / "work"
    prepare_hierarchy_grep_session([top_v], top="top", work_dir=work)
    logs: list[str] = []
    session = prepare_hierarchy_grep_session(
        [top_v],
        top="top",
        work_dir=work,
        on_emit=logs.append,
    )
    assert any("hgrep-cache hit" in line for line in logs)
    assert session.resolve("top.x", top="top")["ok"] is True
    milestone_lines = [line for line in logs if "hgrep-hie milestone" in line]
    assert any("filelist-ready sources=1 top=top" in line for line in milestone_lines)
    assert any("grep-hie-loaded from=cache" in line for line in milestone_lines)
    assert not any("rtl-db-build-start" in line for line in milestone_lines)


def test_emit_hgrep_check_milestones_at_quarter_buckets():
    logs: list[str] = []
    state: dict[str, int] = {}
    total = 8
    _emit_hgrep_check_milestones(0, total, on_emit=logs.append, state=state)
    for done in range(1, total + 1):
        _emit_hgrep_check_milestones(done, total, on_emit=logs.append, state=state)
    joined = "\n".join(logs)
    assert "hierarchy-check-start checks=0/8 pct=0%" in joined
    assert "hierarchy-check checks=2/8 pct=25%" in joined
    assert "hierarchy-check checks=4/8 pct=50%" in joined
    assert "hierarchy-check checks=6/8 pct=75%" in joined
    assert "hierarchy-check checks=8/8 pct=100%" in joined
    assert len(logs) == 5


def test_prepare_hierarchy_grep_session_refresh_cache_rebuilds(tmp_path: Path):
    top_v = _write(tmp_path, "top.v", "module top; wire x; endmodule\n")
    work = tmp_path / "work"
    prepare_hierarchy_grep_session([top_v], top="top", work_dir=work)
    cache = work / GREP_HIE_JSON_NAME
    cache.write_text('{"stale": true}\n', encoding="utf-8")
    logs: list[str] = []
    prepare_hierarchy_grep_session(
        [top_v],
        top="top",
        work_dir=work,
        refresh_cache=True,
        on_emit=logs.append,
    )
    assert any("hgrep-cache clean" in line for line in logs)
    assert any("hgrep-cache write" in line for line in logs)
    assert "module_index" in cache.read_text(encoding="utf-8")


def test_run_hgrep_connect_batch_reuses_session_body_cache(tmp_path: Path, monkeypatch):
    from hierwalk.connect.hierarchy_grep_gate import run_hgrep_connect_batch
    from hierwalk.connect.shared.request import ConnectivityRequest
    from hierwalk.index import DesignIndex
    import hierwalk.hierarchy_grep as hg

    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (output logic out);
          assign out = 1'b0;
        endmodule
        module top;
          child u_a ();
          child u_b ();
        endmodule
        """,
    )
    read_count = 0
    orig_read = hg._read_text

    def counting_read(path):
        nonlocal read_count
        read_count += 1
        return orig_read(path)

    monkeypatch.setattr(hg, "_read_text", counting_read)

    request = ConnectivityRequest(
        checks=(
            ConnectivityCheck("top.u_a.out", "top.u_a.out", check_id="hg1"),
            ConnectivityCheck("top.u_b.out", "top.u_b.out", check_id="hg2"),
            ConnectivityCheck("top.u_a.out", "top.u_b.out", check_id="hg3"),
        ),
        top="top",
    )
    batch, _index, _rows = run_hgrep_connect_batch(
        request,
        [top_v],
        top="top",
        connect_output_dir=tmp_path / "out",
    )
    assert all(r.connected for r in batch.results)
    assert read_count == 1


def test_run_hgrep_connect_batch_skips_full_design_index(tmp_path: Path, monkeypatch):
    from hierwalk.connect.hierarchy_grep_gate import run_hgrep_connect_batch
    from hierwalk.connect.shared.request import ConnectivityRequest
    from hierwalk.index import DesignIndex

    top_v = _write(tmp_path, "top.v", "module top; wire x; endmodule\n")
    other_v = _write(tmp_path, "other.v", "module other; wire y; endmodule\n")
    request = ConnectivityRequest(
        checks=(ConnectivityCheck("top.x", "top.x", check_id="hg1"),),
        top="top",
    )

    def _boom(_sources):
        raise AssertionError("DesignIndex.build must not run for hgrep-only batch")

    monkeypatch.setattr(DesignIndex, "build", staticmethod(_boom))
    logs: list[str] = []
    batch, _index, _rows = run_hgrep_connect_batch(
        request,
        [top_v, other_v],
        top="top",
        connect_output_dir=tmp_path / "out",
        on_emit=logs.append,
    )
    assert batch.results[0].connected
    assert batch.results[0].mode == "hgrep"
    milestone_lines = [line for line in logs if "hgrep-hie milestone" in line]
    assert any("hierarchy-check-start checks=0/1 pct=0%" in line for line in milestone_lines)
    assert any("hierarchy-check checks=1/1 pct=100%" in line for line in milestone_lines)


def test_connect_phase_hgrep_uses_grep_hie_cache(tmp_path: Path):
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
        module top;
          child u_a ();
        endmodule
        """,
    )
    fl = tmp_path / "fl.f"
    fl.write_text(f"{top_v}\n", encoding="utf-8")
    fl_result = parse_filelist(str(fl))
    out_dir = tmp_path / "out"
    request = ConnectivityRequest(
        checks=(ConnectivityCheck("top.u_a.out", "top.u_a.out", check_id="hg1"),),
        top="top",
    )
    run_path_walk_connect(
        request,
        fl_result,
        top="top",
        no_cache=True,
        connect_phase="hgrep",
        connect_output_dir=out_dir,
    )
    assert (out_dir / GREP_HIE_JSON_NAME).is_file()
    log_path = tmp_path / "walk2.log"
    batch, _index, _state = run_path_walk_connect(
        request,
        fl_result,
        top="top",
        no_cache=True,
        connect_phase="hgrep",
        connect_output_dir=out_dir,
        trace_log_path=log_path,
    )
    assert batch.results[0].connected
    assert "hgrep-cache hit" in log_path.read_text(encoding="utf-8")