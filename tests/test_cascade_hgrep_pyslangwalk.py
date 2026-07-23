"""Cascade connect_phase: hgrep then pyslangwalk on survivors only."""

from __future__ import annotations

from pathlib import Path

import pytest

pyslang = pytest.importorskip("pyslang")

from hierwalk.connect.shared.request import parse_connect_request_json
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import run_path_walk_connect
from hierwalk.run_request import HGREP_THEN_PYSLANGWALK, parse_connect_phase_value


def _write(tmp: Path, name: str, text: str) -> str:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return str(p.resolve())


def test_parse_cascade_phase_aliases():
    # Preferred: JSON array (ordered pipeline)
    assert parse_connect_phase_value(["hgrep", "pyslangwalk"]) == HGREP_THEN_PYSLANGWALK
    assert parse_connect_phase_value(["hgrep", "pyslang"]) == HGREP_THEN_PYSLANGWALK
    # Legacy string join
    assert parse_connect_phase_value("hgrep+pyslangwalk") == HGREP_THEN_PYSLANGWALK
    assert parse_connect_phase_value("hgrep-pyslangwalk") == HGREP_THEN_PYSLANGWALK
    assert parse_connect_phase_value("hgrep-then-pyslangwalk") == HGREP_THEN_PYSLANGWALK


def test_cascade_skips_pyslangwalk_on_hgrep_miss(tmp_path: Path):
    rtl = _write(
        tmp_path,
        "top.sv",
        """
        module leaf (input logic [3:0] d, output logic [3:0] q);
          assign q = d;
        endmodule
        module top;
          logic [1:0][3:0] bus;
          logic [3:0] s0;
          leaf u0 (.d(bus[0]), .q(s0));
        endmodule
        """,
    )
    (tmp_path / "filelist.f").write_text("top.sv\n", encoding="utf-8")
    fl = parse_filelist(str(tmp_path / "filelist.f"), index_cwd=str(tmp_path))
    req = parse_connect_request_json(
        {
            "top": "top",
            "include_ff": True,
            "checks": [
                {"id": "ok", "a": "top.s0", "b": "top.bus"},
                {"id": "typo", "a": "top.NOPE", "b": "top.bus"},
            ],
        }
    )
    logs: list[str] = []
    work = tmp_path / "db"
    batch, _index, _state = run_path_walk_connect(
        req,
        fl,
        top="top",
        connect_phase="hgrep+pyslangwalk",
        connect_output_dir=work,
        connect_output_name="conn.tsv",
        no_cache=True,
        on_progress=logs.append,
    )
    by = {r.check_id: r for r in batch.results}
    assert "typo" in by
    assert not by["typo"].connected
    assert by["typo"].mode == "hgrep"
    # Survivor goes through pyslangwalk (mode may be pyslangwalk or +text).
    assert by["ok"].mode.startswith("pyslangwalk") or "pyslangwalk" in (
        by["ok"].note or ""
    )
    assert any("cascade" in m and "hgrep" in m for m in logs)
    assert any("→ pyslangwalk" in m or "pyslangwalk" in m for m in logs)
    # Electrical report from pyslangwalk stage
    assert (work / "pyslangwalk.report").is_file()
    report = (work / "pyslangwalk.report").read_text(encoding="utf-8")
    # typo should not appear as electrical work on NOPE if filtered — survivors only
    # ok path may PASS electrical
    assert "top.s0" in report or "ok" in report


def test_suite_schedules_cascade_phase(tmp_path: Path):
    from hierwalk.run_tests import (
        build_test_run_configs,
        expand_suite_verification_plan,
        parse_flat_run_suite,
    )

    rtl = _write(tmp_path, "top.sv", "module top; endmodule\n")
    (tmp_path / "filelist.f").write_text("top.sv\n", encoding="utf-8")
    doc = {
        "filelist": "filelist.f",
        "top": "top",
        "index-cwd": str(tmp_path),
        "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "connect_phase": ["hgrep", "pyslangwalk"],
            "checks": [{"id": "c", "a": "top.a", "b": "top.b"}],
        },
    }
    suite = parse_flat_run_suite(doc, raw_text=None, base_dir=tmp_path)
    plan = build_test_run_configs(suite, doc, base_dir=tmp_path)
    expanded = expand_suite_verification_plan(plan)
    assert len(expanded) == 1
    _e, cfg = expanded[0]
    assert cfg.verification_phase == HGREP_THEN_PYSLANGWALK
    assert cfg.mode == "check-pyslangwalk"
