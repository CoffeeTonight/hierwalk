"""Strict enable gate: disabled run_on_full_index must not run full-filelist index."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hierwalk.cli_execute import execute_run
from hierwalk.run_tests import (
    RUN_ON_FULL_INDEX,
    build_test_run_configs,
    parse_flat_run_suite,
)
from hierwalk.run_request import RunConfig


CONE_RTL = """
module top(input logic clk, input logic a, output logic z);
  wire mid;
  assign mid = a;
  mid u_m (.clk(clk), .din(mid), .qout(z));
endmodule
module mid(input logic clk, input logic din, output logic qout);
  logic r;
  always_ff @(posedge clk) r <= din;
  assign qout = r;
endmodule
"""


def test_suite_never_schedules_disabled_full_index_step():
    doc = {
        "filelist": "fl.f",
        "top": "top",
        "run_on_full_index": {"enable": 0, "mode": "hierarchy", "output": "inst.tsv"},
        "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "checks": [{"id": "t", "a": "top.a", "b": "top.z"}],
        },
    }
    suite = parse_flat_run_suite(doc)
    kinds = [entry.kind for entry in suite.tests]
    assert RUN_ON_FULL_INDEX not in kinds
    assert suite.full_index_enabled is False


def test_execute_run_uses_path_walk_not_full_index_loader(tmp_path: Path):
    rtl = tmp_path / "d.v"
    rtl.write_text(CONE_RTL, encoding="utf-8")
    fl = tmp_path / "fl.f"
    fl.write_text(f"{rtl.resolve()}\n", encoding="utf-8")
    doc = {
        "filelist": str(fl),
        "top": "top",
        "run_on_full_index": {"enable": 0, "mode": "hierarchy"},
        "run_conn_check": {
            "enable": 1,
            "mode": "full-index",
            "checks": [{"id": "t", "a": "top.a", "b": "top.z"}],
            "output": "-",
        },
    }
    suite = parse_flat_run_suite(doc, base_dir=tmp_path)
    _, cfg = build_test_run_configs(suite, doc, base_dir=tmp_path)[0]
    assert cfg.index_strategy == "path-walk"

    class _Ap:
        def error(self, msg):
            raise SystemExit(msg)

    with patch("hierwalk.cli_execute.load_or_build_index") as load_full:
        load_full.side_effect = AssertionError(
            "load_or_build_index called while run_on_full_index.enable is 0"
        )
        rc = execute_run(cfg, _Ap())
    assert rc == 0
    load_full.assert_not_called()


def test_cli_stderr_has_no_hierarchy_mode_for_disabled_full_index(tmp_path: Path):
    rtl = tmp_path / "d.v"
    rtl.write_text(CONE_RTL, encoding="utf-8")
    fl = tmp_path / "fl.f"
    fl.write_text(f"{rtl.resolve()}\n", encoding="utf-8")
    run_json = tmp_path / "suite.json"
    run_json.write_text(
        json.dumps(
            {
                "filelist": str(fl.name),
                "top": "top",
                "run_on_full_index": {
                    "enable": 0,
                    "mode": "hierarchy",
                    "output": "instances.tsv",
                },
                "run_conn_check": {
                    "enable": 1,
                    "mode": "path-walk",
                    "checks": [{"id": "t", "a": "top.a", "b": "top.z"}],
                    "output": "-",
                },
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["hier-walk", str(run_json)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    err = proc.stderr
    assert "inactive run_on_full_index (enable: 0" in err
    assert "kind=run_on_full_index" not in err
    assert "index: building from" not in err
    assert "Mode:          hierarchy" not in err
    assert not (tmp_path / "instances.tsv").exists()


def test_enabled_alias_zero_skips_full_index_step():
    doc = {
        "filelist": "fl.f",
        "top": "top",
        "run_on_full_index": {"enabled": 0, "mode": "hierarchy"},
        "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "checks": [{"id": "t", "a": "top.a", "b": "top.z"}],
        },
    }
    suite = parse_flat_run_suite(doc)
    assert suite.full_index_enabled is False
    assert all(e.kind != RUN_ON_FULL_INDEX for e in suite.tests)


def test_legacy_top_level_mode_hierarchy_blocked_when_full_index_disabled(tmp_path):
    """Top-level mode:hierarchy must not run when only full_index block is disabled."""
    run_json = tmp_path / "legacy.json"
    run_json.write_text(
        json.dumps(
            {
                "filelist": "fl.f",
                "top": "top",
                "mode": "hierarchy",
                "run_on_full_index": {"enable": 0, "mode": "hierarchy"},
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["hier-walk", str(run_json)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "no enabled steps" in proc.stderr + proc.stdout


def test_missing_full_index_enable_defaults_off_when_verifications_present():
    """Verification block enable does not imply run_on_full_index is off — needs its own field."""
    doc = {
        "filelist": "fl.f",
        "top": "top",
        "run_on_full_index": {"mode": "hierarchy", "output": "inst.tsv", "jobs": 4},
        "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "checks": [{"id": "t", "a": "top.a", "b": "top.z"}],
        },
        "run_io_trace": {
            "enable": 1,
            "mode": "path-walk",
            "instance": "top.u0",
        },
        "run_cone_trace": {"enable": 0, "mode": "path-walk", "fanout_cone": "top.a"},
    }
    suite = parse_flat_run_suite(doc)
    assert suite.full_index_enabled is False
    assert all(e.kind != RUN_ON_FULL_INDEX for e in suite.tests)
    assert len(suite.tests) == 2


def test_top_level_enable_zero_disables_full_index_block():
    doc = {
        "filelist": "fl.f",
        "top": "top",
        "enable": 0,
        "run_on_full_index": {"mode": "hierarchy", "output": "inst.tsv"},
        "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "checks": [{"id": "t", "a": "top.a", "b": "top.z"}],
        },
    }
    suite = parse_flat_run_suite(doc)
    assert suite.full_index_enabled is False
    assert all(e.kind != RUN_ON_FULL_INDEX for e in suite.tests)
    assert any("top-level enable" in w for w in suite.enable_warnings)


def test_jsonc_commented_enable_treated_as_disabled():
    raw = """
    {
      "filelist": "fl.f",
      "top": "top",
      "run_on_full_index": {
        // "enable": 0,
        "mode": "hierarchy"
      },
      "run_conn_check": {
        "enable": 1,
        "mode": "path-walk",
        "checks": [{"id": "t", "a": "top.a", "b": "top.z"}]
      }
    }
    """
    from hierwalk.run_request import loads_json_document

    doc = loads_json_document(raw)
    suite = parse_flat_run_suite(doc, raw_text=raw)
    assert suite.full_index_enabled is False
    assert any("JSONC comment" in w for w in suite.enable_warnings)


def test_nested_enable_under_settings_is_honored_with_warning():
    doc = {
        "filelist": "fl.f",
        "top": "top",
        "run_on_full_index": {
            "mode": "hierarchy",
            "settings": {"enable": 0},
        },
        "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "checks": [{"id": "t", "a": "top.a", "b": "top.z"}],
        },
    }
    suite = parse_flat_run_suite(doc)
    assert suite.full_index_enabled is False
    assert any("settings" in w for w in suite.enable_warnings)


def test_duplicate_enable_key_last_value_wins_and_warns():
    from hierwalk.run_request import loads_json_document

    raw = (
        '{"filelist":"fl.f","top":"top",'
        '"run_on_full_index":{"enable":0,"mode":"hierarchy","enable":1},'
        '"run_conn_check":{"enable":1,"mode":"path-walk",'
        '"checks":[{"id":"t","a":"top.a","b":"top.z"}]}}'
    )
    audit: list[str] = []
    doc = loads_json_document(raw, audit=audit)
    suite = parse_flat_run_suite(doc, raw_text=raw)
    assert any(e.kind == RUN_ON_FULL_INDEX for e in suite.tests)
    assert audit


def test_filelist_json_path_runs_suite_not_legacy_hierarchy(tmp_path: Path):
    """hier-walk run.json (no -c) must flat-suite parse, not fallback hierarchy."""
    rtl = tmp_path / "d.v"
    rtl.write_text(CONE_RTL, encoding="utf-8")
    fl = tmp_path / "fl.f"
    fl.write_text(f"{rtl.resolve()}\n", encoding="utf-8")
    run_json = tmp_path / "suite.json"
    run_json.write_text(
        json.dumps(
            {
                "filelist": str(fl.name),
                "top": "top",
                "run_on_full_index": {
                    "enable": 0,
                    "mode": "hierarchy",
                    "output": "instances.tsv",
                },
                "run_conn_check": {
                    "enable": 1,
                    "mode": "path-walk",
                    "checks": [{"id": "t", "a": "top.a", "b": "top.z"}],
                    "output": "-",
                },
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["hier-walk", str(run_json)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    err = proc.stderr
    assert "test-suite 1 verification block(s), text then logical" in err
    assert "enable-audit: block=run_on_full_index raw_enable=0 parsed_enable=0 action=SKIP" in err
    assert "effective_mode=hierarchy" not in err
    assert "index_loader=load_or_build_index" not in err


def test_inferred_hierarchy_blocked_without_full_index_step():
    doc = {
        "filelist": "fl.f",
        "top": "top",
        "run_on_full_index": {"enable": 0, "mode": "hierarchy"},
        "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "checks": [{"id": "t", "a": "top.a", "b": "top.z"}],
        },
    }
    suite = parse_flat_run_suite(doc)
    _, cfg = build_test_run_configs(suite, doc)[0]

    class _Ap:
        def error(self, msg):
            raise SystemExit(msg)

    # Simulate legacy fallback cfg (mode unset → inferred hierarchy)
    legacy = RunConfig(filelist=cfg.filelist, top=cfg.top, mode=None)
    with pytest.raises(SystemExit, match="hierarchy/search/find-top blocked"):
        execute_run(legacy, _Ap())


def test_enable_audit_before_jobs_line_in_stderr_order(tmp_path: Path):
    rtl = tmp_path / "d.v"
    rtl.write_text(CONE_RTL, encoding="utf-8")
    fl = tmp_path / "fl.f"
    fl.write_text(f"{rtl.resolve()}\n", encoding="utf-8")
    run_json = tmp_path / "suite.json"
    run_json.write_text(
        json.dumps(
            {
                "filelist": str(fl.name),
                "top": "top",
                "run_on_full_index": {
                    "enable": 0,
                    "mode": "hierarchy",
                    "jobs": 4,
                    "output": "instances.tsv",
                },
                "run_conn_check": {
                    "enable": 1,
                    "mode": "path-walk",
                    "checks": [{"id": "t", "a": "top.a", "b": "top.z"}],
                    "output": "-",
                },
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["hier-walk", str(run_json)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    lines = proc.stderr.splitlines()
    audit_i = next(i for i, l in enumerate(lines) if "enable-audit:" in l)
    jobs_i = next(i for i, l in enumerate(lines) if l.startswith("run: config="))
    assert audit_i < jobs_i
    assert "source=json:run_on_full_index.jobs" not in proc.stderr


def test_verify_enable_gate_json_subprocess():
    """Bundled verify_enable_gate.json: full_index off, 2 path-walk steps on."""
    root = Path(__file__).resolve().parents[1] / "examples" / "stress_seed42"
    cfg = root / "verify_enable_gate.json"
    assert cfg.is_file()
    for name in (
        "VERIFY_gate_instances.tsv",
        "VERIFY_gate_conn.tsv",
        "VERIFY_gate_trace.tsv",
        "VERIFY_gate_cone.tsv",
    ):
        p = root / name
        if p.exists():
            p.unlink()
    proc = subprocess.run(
        ["hier-walk", str(cfg)],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    err = proc.stderr
    assert "enable-audit: block=run_on_full_index raw_enable=0 parsed_enable=0 action=SKIP" in err
    assert "enable-trace: block=run_on_full_index raw_enable=0 parsed_enable=0 action=SKIP" in err
    assert "enable-trace: block=run_conn_check raw_enable=1 parsed_enable=1 action=SCHEDULE" in err
    assert "enable-trace: block=run_io_trace raw_enable=1 parsed_enable=1 action=SCHEDULE" in err
    assert "enable-trace: block=run_cone_trace raw_enable=0 parsed_enable=0 action=SKIP" in err
    assert "kind=run_on_full_index" not in err
    assert "index_loader=load_or_build_index" not in err
    assert "index: building from" not in err
    assert (root / ".db_stress_top" / "conn.text.tsv").is_file()
    assert (root / ".db_stress_top" / "conn.tsv").is_file()
    assert (root / "VERIFY_gate_trace.text.tsv").is_file()
    assert (root / "VERIFY_gate_trace.tsv").is_file()
    assert not (root / "VERIFY_gate_instances.tsv").exists()
    assert not (root / "VERIFY_gate_cone.tsv").exists()