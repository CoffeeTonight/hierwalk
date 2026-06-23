"""Verification step artifacts must exist immediately after each phase completes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hierwalk.cli_execute import execute_run
from hierwalk.connect_artifacts import connect_output_paths, missing_verification_artifacts
from hierwalk.filelist import parse_filelist
from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.path_walk import run_path_walk_connect
from hierwalk.run_request import RunConfig
from hierwalk.run_tests import build_test_run_configs, expand_suite_verification_plan, parse_flat_run_suite


def _minimal_conn_fl(tmp_path: Path):
    (tmp_path / "top.v").write_text(
        "module top(input logic clk); child u0(.clk(clk)); endmodule\n"
        "module child(input logic clk); endmodule\n",
        encoding="utf-8",
    )
    fl = tmp_path / "fl.f"
    fl.write_text(f"{(tmp_path / 'top.v').resolve()}\n", encoding="utf-8")
    return parse_filelist(str(fl), index_cwd=str(tmp_path))


def test_text_tsv_exists_before_timing_step_ends(tmp_path: Path):
    """conn.text.tsv must be on disk when connect-coi timing step completes."""
    flr = _minimal_conn_fl(tmp_path)
    db = tmp_path / ".db_top"
    req = ConnectivityRequest(checks=(ConnectivityCheck("top.clk", "top.u0.clk"),), top="top")
    run_path_walk_connect(
        req,
        flr,
        top="top",
        no_cache=True,
        connect_phase="text",
        connect_output_dir=db,
    )
    text_path = db / "conn.text.tsv"
    assert text_path.is_file()
    assert text_path.stat().st_size > 0
    assert "connected_text" in text_path.read_text(encoding="utf-8")


def test_text_tsv_written_even_with_zero_checks(tmp_path: Path):
    flr = _minimal_conn_fl(tmp_path)
    db = tmp_path / ".db_top"
    req = ConnectivityRequest(checks=(), top="top")
    run_path_walk_connect(
        req,
        flr,
        top="top",
        no_cache=True,
        connect_phase="text",
        connect_output_dir=db,
    )
    text_path = db / "conn.text.tsv"
    assert text_path.is_file()
    assert text_path.stat().st_size > 0


def test_suite_text_step_creates_text_tsv_before_logical(tmp_path: Path):
    _minimal_conn_fl(tmp_path)
    doc = {
        "filelist": "fl.f",
        "top": "top",
        "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "checks": [{"id": "t", "a": "top.clk", "b": "top.u0.clk"}],
        },
    }
    run_json = tmp_path / "run.json"
    run_json.write_text(json.dumps(doc), encoding="utf-8")
    suite = parse_flat_run_suite(doc, base_dir=tmp_path)
    plan = expand_suite_verification_plan(
        build_test_run_configs(suite, doc, base_dir=tmp_path)
    )
    text_entry, text_cfg = plan[0]
    assert text_cfg.verification_phase == "text"
    assert text_entry is not None

    class _Ap:
        def error(self, msg: str) -> None:
            raise AssertionError(msg)

    rc = execute_run(text_cfg, _Ap())
    assert rc == 0
    work = tmp_path / ".db_top"
    paths = connect_output_paths(work, text_cfg.output)
    assert paths.text_tsv.is_file()
    assert not paths.logical_tsv.is_file()
    assert missing_verification_artifacts(text_cfg, work) == []


def test_suite_logical_step_creates_logical_tsv(tmp_path: Path):
    _minimal_conn_fl(tmp_path)
    doc = {
        "filelist": "fl.f",
        "top": "top",
        "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "checks": [{"id": "t", "a": "top.clk", "b": "top.u0.clk"}],
        },
    }
    suite = parse_flat_run_suite(doc, base_dir=tmp_path)
    plan = expand_suite_verification_plan(
        build_test_run_configs(suite, doc, base_dir=tmp_path)
    )
    logical_cfg = plan[1][1]
    assert logical_cfg.verification_phase == "logical"

    class _Ap:
        def error(self, msg: str) -> None:
            raise AssertionError(msg)

    rc = execute_run(logical_cfg, _Ap())
    assert rc == 0
    work = tmp_path / ".db_top"
    paths = connect_output_paths(work, logical_cfg.output)
    assert paths.logical_tsv.is_file()
    assert missing_verification_artifacts(logical_cfg, work) == []


def test_subprocess_text_phase_artifact_before_step_return(tmp_path: Path):
    doc = {
        "filelist": "fl.f",
        "top": "top",
        "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "checks": [{"id": "t", "a": "top.clk", "b": "top.u0.clk"}],
        },
    }
    (tmp_path / "top.v").write_text(
        "module top(input logic clk); child u0(.clk(clk)); endmodule\n"
        "module child(input logic clk); endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "fl.f").write_text(f"{(tmp_path / 'top.v').resolve()}\n", encoding="utf-8")
    (tmp_path / "run.json").write_text(json.dumps(doc), encoding="utf-8")
    proc = subprocess.run(
        ["hier-walk", "run.json"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    text_path = tmp_path / ".db_top" / "conn.text.tsv"
    logical_path = tmp_path / ".db_top" / "conn.tsv"
    assert text_path.is_file(), proc.stderr
    assert logical_path.is_file(), proc.stderr
    assert "connect-text-conn written" in proc.stderr
    assert "connect-logical-conn written" in proc.stderr