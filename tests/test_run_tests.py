"""Flat run JSON: run_on_full_index + run_conn_check / run_io_trace / run_cone_trace."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hierwalk.run_request import (
    load_run_request_with_jobs_source,
    loads_json_document,
    resolve_connectivity_request,
)
from hierwalk.run_tests import (
    RUN_CONN_CHECK,
    RUN_CONE_TRACE,
    RUN_IO_TRACE,
    RUN_ON_FULL_INDEX,
    build_test_run_configs,
    expand_suite_verification_plan,
    list_disabled_suite_blocks,
    parse_enable,
    parse_flat_run_suite,
    parse_run_test_suite,
    resolve_verification_index_strategy,
    run_config_for_test,
    spec_for_test_entry,
)


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


def test_loads_json_document_strips_line_comments():
    doc = loads_json_document(
        """
        {
          // block comment line
          "filelist": "fl.f",
          "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "checks": [{"id": "t", "a": "top.a", "b": "top.z"}]
          }
        }
        """
    )
    assert doc["filelist"] == "fl.f"
    suite = parse_flat_run_suite(doc)
    assert len(suite.tests) == 1


def test_parse_enable_accepts_one_zero():
    assert parse_enable(1) is True
    assert parse_enable(0) is False
    assert parse_enable("1") is True
    assert parse_enable("0") is False


def test_suite_loader_does_not_infer_hierarchy_when_full_index_disabled(tmp_path):
    doc = {
        "filelist": "design.f",
        "top": "top",
        "run_on_full_index": {
            "enable": 0,
            "mode": "hierarchy",
            "jobs": 4,
        },
        "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "checks": [{"id": "a", "a": "top.a", "b": "top.z"}],
        },
    }
    run_json = tmp_path / "suite.json"
    run_json.write_text(json.dumps(doc), encoding="utf-8")
    cfg, jobs_src = load_run_request_with_jobs_source(run_json)
    assert cfg.mode is None
    assert jobs_src is None
    assert cfg.jobs == 0
    suite = parse_flat_run_suite(doc)
    assert len(suite.tests) == 1
    assert suite.tests[0].kind == RUN_CONN_CHECK
    assert list_disabled_suite_blocks(doc) == ("run_on_full_index",)


def test_parse_flat_suite_with_full_db_and_three_tests():
    doc = {
        "filelist": "design.f",
        "top": "top",
        "run_on_full_index": {
            "enable": 0,
            "mode": "hierarchy",
            "ignore_path": ["pcielinktop"],
            "jobs": 4,
            "output": "instances.tsv",
        },
        "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "checks": [{"id": "a", "a": "top.a", "b": "top.z"}],
            "output": "conn.tsv",
        },
        "run_io_trace": {
            "enable": 1,
            "mode": "full-index",
            "instance": "top.u_m",
            "direction": "driver",
            "path_kind": "ff",
            "output": "trace.tsv",
        },
        "run_cone_trace": {
            "enable": 0,
            "mode": "full-index",
            "fanout_cone": "top.u_m.din",
            "output": "cone.tsv",
        },
    }
    suite = parse_flat_run_suite(doc, base_dir="/tmp")
    assert suite.full_index_spec is not None
    assert len(suite.tests) == 2
    assert suite.tests[0].kind == RUN_CONN_CHECK
    assert suite.tests[1].kind == RUN_IO_TRACE

    plans = build_test_run_configs(suite, doc, base_dir="/tmp")
    assert len(plans) == 2

    conn_entry, conn_cfg = plans[0]
    assert conn_entry.kind == RUN_CONN_CHECK
    assert conn_entry.mode == "path-walk"
    assert conn_cfg.mode == "check-connect-batch"
    assert conn_cfg.index_strategy == "path-walk"
    assert conn_cfg.output == "conn.tsv"
    assert conn_cfg.ignore_path == ()
    assert conn_cfg.jobs == 0
    _, trace_cfg = plans[1]
    assert trace_cfg.index_strategy == "path-walk"
    req = resolve_connectivity_request(conn_cfg)
    assert req is not None
    assert req.checks[0].check_id == "a"


def test_expand_suite_verification_plan_text_then_logical():
    doc = {
        "filelist": "design.f",
        "top": "top",
        "run_on_full_index": {"enable": 1, "mode": "hierarchy", "output": "inst.tsv"},
        "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "checks": [{"id": "a", "a": "top.a", "b": "top.b"}],
        },
        "run_io_trace": {
            "enable": 1,
            "mode": "path-walk",
            "instance": "top.u_m",
            "output": "io.tsv",
        },
    }
    suite = parse_flat_run_suite(doc, base_dir="/tmp")
    base_plan = build_test_run_configs(suite, doc, base_dir="/tmp")
    expanded = expand_suite_verification_plan(base_plan)
    kinds = [entry.kind if entry else "legacy" for entry, _ in expanded]
    assert kinds == [
        RUN_ON_FULL_INDEX,
        RUN_CONN_CHECK,
        RUN_IO_TRACE,
        RUN_CONN_CHECK,
        RUN_IO_TRACE,
    ]
    phases = [cfg.verification_phase for _, cfg in expanded]
    assert phases == ["both", "text", "text", "logical", "logical"]


def test_run_on_full_index_step_when_enabled():
    doc = {
        "filelist": "design.f",
        "top": "top",
        "run_on_full_index": {
            "enable": 1,
            "mode": "hierarchy",
            "ignore_module": ["bb_mod"],
            "output": "inst.tsv",
        },
        "run_conn_check": {"enable": 0, "mode": "check-connect-batch", "checks": []},
    }
    suite = parse_flat_run_suite(doc)
    assert len(suite.tests) == 1
    assert suite.tests[0].kind == RUN_ON_FULL_INDEX
    _, cfg = build_test_run_configs(suite, doc)[0]
    assert cfg.mode == "hierarchy"
    assert cfg.ignore_module == ("bb_mod",)


def test_run_conn_check_ignore_path_survives_empty_full_index_merge():
    """run_on_full_index ignore-path: [] must not wipe per-step ignores."""
    doc = {
        "filelist": "design.f",
        "top": "top",
        "run_on_full_index": {
            "enable": 1,
            "mode": "hierarchy",
            "ignore-path": [],
            "output": "inst.tsv",
        },
        "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "ignore-path": ["DW_*"],
            "checks": [{"a": "top.a", "b": "top.b"}],
        },
    }
    suite = parse_flat_run_suite(doc)
    conn_cfg = next(
        cfg
        for ent, cfg in build_test_run_configs(suite, doc)
        if ent.kind == RUN_CONN_CHECK
    )
    assert conn_cfg.ignore_path == ("DW_*",)


def test_legacy_run_on_full_db_key_still_parses():
    doc = {
        "filelist": "fl.f",
        "top": "top",
        "run_on_full_db": {
            "enable": 1,
            "mode": "hierarchy",
            "output": "inst.tsv",
        },
    }
    suite = parse_flat_run_suite(doc)
    assert suite.tests[0].kind == RUN_ON_FULL_INDEX
    assert suite.full_index_spec is not None


def test_legacy_verification_modes_map_to_full_index():
    doc = {
        "filelist": "fl.f",
        "top": "top",
        "run_conn_check": {
            "enable": 1,
            "mode": "check-connect-batch",
            "checks": [{"id": "t", "a": "top.a", "b": "top.z"}],
        },
    }
    suite = parse_flat_run_suite(doc)
    assert suite.tests[0].mode == "full-index"
    _, cfg = build_test_run_configs(suite, doc)[0]
    assert cfg.mode == "check-connect-batch"


def test_disabled_full_index_blocks_verification_full_index_strategy():
    doc = {
        "filelist": "fl.f",
        "top": "top",
        "run_on_full_index": {"enable": 0, "mode": "hierarchy"},
        "run_conn_check": {
            "enable": 1,
            "mode": "full-index",
            "checks": [{"id": "t", "a": "top.a", "b": "top.z"}],
        },
    }
    suite = parse_flat_run_suite(doc)
    assert suite.full_index_enabled is False
    assert suite.tests[0].mode == "full-index"
    _, cfg = build_test_run_configs(suite, doc)[0]
    assert cfg.index_strategy == "path-walk"
    assert (
        resolve_verification_index_strategy(
            "full-index",
            full_index_spec=suite.full_index_spec,
            full_index_enabled=False,
        )
        == "path-walk"
    )


def test_disabled_full_index_does_not_merge_settings():
    doc = {
        "filelist": "fl.f",
        "top": "top",
        "run_on_full_index": {
            "enable": 0,
            "ignore_path": ["skip_me"],
            "jobs": 8,
            "no_cache": True,
        },
        "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "checks": [{"id": "t", "a": "top.a", "b": "top.z"}],
        },
    }
    suite = parse_flat_run_suite(doc)
    _, cfg = build_test_run_configs(suite, doc)[0]
    assert cfg.ignore_path == ()
    assert cfg.jobs == 0
    assert cfg.no_cache is False


def test_enabled_full_index_settings_merge_into_verification():
    doc = {
        "filelist": "fl.f",
        "top": "top",
        "run_on_full_index": {
            "enable": 1,
            "mode": "hierarchy",
            "ignore_path": ["skip_me"],
            "jobs": 8,
            "output": "inst.tsv",
        },
        "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "checks": [{"id": "t", "a": "top.a", "b": "top.z"}],
        },
    }
    suite = parse_flat_run_suite(doc)
    assert len(suite.tests) == 2
    conn_plan = build_test_run_configs(suite, doc)[1]
    _, cfg = conn_plan
    assert cfg.ignore_path == ("skip_me",)
    assert cfg.jobs == 8


def test_legacy_tests_array_still_works():
    doc = {
        "filelist": "design.f",
        "top": "top",
        "tests": [
            {
                "run_conn_check": {
                    "enable": 1,
                    "mode": "check-connect-batch",
                    "checks": [{"id": "a", "a": "top.a", "b": "top.z"}],
                },
            },
        ],
    }
    suite = parse_run_test_suite(doc)
    assert len(suite.tests) == 1


def test_cli_runs_flat_suite(tmp_path: Path):
    rtl = tmp_path / "d.v"
    rtl.write_text(CONE_RTL, encoding="utf-8")
    fl = tmp_path / "fl.f"
    fl.write_text(f"{rtl.resolve()}\n", encoding="utf-8")
    run_json = tmp_path / "suite.run.json"
    run_json.write_text(
        json.dumps(
            {
                "filelist": "fl.f",
                "top": "top",
                "run_on_full_index": {"enable": 0, "mode": "hierarchy"},
                "run_conn_check": {
                    "enable": 1,
                    "mode": "path-walk",
                    "checks": [{"id": "t", "a": "top.a", "b": "top.z"}],
                },
                "run_io_trace": {
                    "enable": 1,
                    "mode": "full-index",
                    "instance": "top.u_m",
                    "direction": "driver",
                    "path_kind": "ff",
                },
                "run_cone_trace": {
                    "enable": 1,
                    "mode": "full-index",
                    "fanout_cone": "top.u_m.din",
                },
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["hier-walk", str(run_json)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "test-suite 3 verification block(s), text then logical" in proc.stderr
    assert "phase=text" in proc.stderr
    assert "phase=logical" in proc.stderr
    assert "inactive run_on_full_index (enable: 0" in proc.stderr
    assert "run: mode=hierarchy" not in proc.stderr
    assert "index: building from" not in proc.stderr
    assert "kind=run_conn_check" in proc.stderr
    assert "mode=path-walk" in proc.stderr
    assert "kind=run_io_trace" in proc.stderr
    assert "kind=run_cone_trace" in proc.stderr