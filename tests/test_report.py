"""End-of-run report formatting."""

from __future__ import annotations

from pathlib import Path

from hierwalk.filelist import parse_filelist
from hierwalk.index import DesignIndex
from hierwalk.preprocess import preprocess_file
from hierwalk.models import ConnectEndpoint, ConnectResult
from hierwalk.report import RunReport, default_log_path, emit_run_report, write_run_report_log


def test_run_report_contains_key_fields(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text("module top; endmodule\n", encoding="utf-8")
    fl_path = tmp_path / "design.f"
    fl_path.write_text(f"{rtl}\n", encoding="utf-8")
    fl = parse_filelist(fl_path)
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})

    cache_file = tmp_path / "cache.pkl"
    cache_file.write_bytes(b"x" * 2048)
    report = RunReport(
        filelist_path=str(fl_path),
        elapsed_sec=1.234,
        fl=fl,
        index=index,
        cache_path=cache_file,
        cache_enabled=True,
        index_cache_hit=True,
        index_rebuilt=False,
        elab_tops=["top"],
        elab_cache_hits=1,
        instance_rows=1,
        mode="hierarchy",
    )
    body = "\n".join(report.lines())
    assert "=== hier-walk report ===" in body
    assert "--- summary ---" in body
    assert "Elapsed:" in body
    assert "RTL sources:" in body
    assert "Modules:" in body
    assert "Filelist linking" in body
    assert "Index cache" in body
    assert "Size:" in body
    assert "Instances:" in body


def test_run_report_lists_connect_endpoints_on_fail(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text("module top; endmodule\n", encoding="utf-8")
    fl_path = tmp_path / "design.f"
    fl_path.write_text(f"{rtl}\n", encoding="utf-8")
    fl = parse_filelist(fl_path)
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    result = ConnectResult(
        endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
        endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
        connected=False,
        mode="port-port",
        connected_text=False,
        connected_logical=False,
        note="no path",
        check_id="chk1",
    )
    body = "\n".join(
        RunReport(
            filelist_path=str(fl_path),
            elapsed_sec=0.1,
            fl=fl,
            index=index,
            mode="path-walk",
            connect_results=[result],
            connect_phase="text",
        ).lines()
    )
    assert "Connectivity" in body
    assert "top.a -> top.b" in body
    assert "coi" in body
    assert "FAIL" in body
    assert "[chk1]" in body
    assert "no path" in body


def test_default_log_path_next_to_output(tmp_path):
    out = tmp_path / "hier.tsv"
    fl = tmp_path / "design.f"
    assert default_log_path(str(fl), str(out)) == tmp_path / "hier.tsv.hier-walk.log"
    assert (
        default_log_path(str(fl), str(out), phase="text")
        == tmp_path / "hier.text.hier-walk.log"
    )


def test_run_report_shows_connectivity_when_phase_set_without_results(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text("module top; endmodule\n", encoding="utf-8")
    fl_path = tmp_path / "design.f"
    fl_path.write_text(f"{rtl}\n", encoding="utf-8")
    fl = parse_filelist(fl_path)
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    body = "\n".join(
        RunReport(
            filelist_path=str(fl_path),
            elapsed_sec=0.1,
            fl=fl,
            index=index,
            mode="path-walk",
            connect_phase="text",
        ).lines()
    )
    assert "Connectivity" in body
    assert "Phase:         text" in body
    assert "(no checks)" in body


def test_emit_run_report_writes_log(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text("module top; endmodule\n", encoding="utf-8")
    fl_path = tmp_path / "design.f"
    fl_path.write_text(f"{rtl}\n", encoding="utf-8")
    fl = parse_filelist(fl_path)
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    log_file = tmp_path / "run.log"

    emit_run_report(
        RunReport(
            filelist_path=str(fl_path),
            elapsed_sec=0.5,
            fl=fl,
            index=index,
            mode="find-top",
            top_candidates=1,
        ),
        log_path=log_file,
        announce_log=False,
    )
    assert log_file.is_file()
    content = log_file.read_text(encoding="utf-8")
    assert "hier-walk report" in content
    assert "Elapsed:" in content