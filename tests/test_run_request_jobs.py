"""Run request jobs parsing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hierwalk.cli import _build_parser
from hierwalk.run_request import (
    load_run_request,
    merge_options_from_connect_batch_json,
    merge_run_config,
    parse_run_request_json,
    run_config_from_args,
    try_load_run_request_from_path,
)


def test_jobs_alias_j_in_json():
    cfg = parse_run_request_json(
        {"filelist": "top.f", "j": 16},
        base_dir="/tmp",
    )
    assert cfg.jobs == 16


def test_jobs_field_takes_precedence_over_j():
    cfg = parse_run_request_json(
        {"filelist": "top.f", "jobs": 8, "j": 16},
        base_dir="/tmp",
    )
    assert cfg.jobs == 8


def test_ignore_filelist_hyphen_alias_in_json():
    cfg = parse_run_request_json(
        {
            "filelist": "top.f",
            "ignore-filelist": ["pcie_block.f", "phy_rtl.f"],
        },
        base_dir="/tmp",
    )
    assert cfg.ignore_filelist == ("pcie_block.f", "phy_rtl.f")


def test_ignore_path_hyphen_alias_in_json():
    cfg = parse_run_request_json(
        {
            "filelist": "top.f",
            "ignore-path": ["pcielinktop", "pciephyyop"],
        },
        base_dir="/tmp",
    )
    assert cfg.ignore_path == ("pcielinktop", "pciephyyop")


def test_jobs_alias_job_singular_in_json():
    cfg = parse_run_request_json(
        {"filelist": "top.f", "job": 16},
        base_dir="/tmp",
    )
    assert cfg.jobs == 16


def test_jobs_string_value_in_json():
    cfg = parse_run_request_json(
        {"filelist": "top.f", "jobs": "16"},
        base_dir="/tmp",
    )
    assert cfg.jobs == 16


def test_merge_keeps_json_jobs_when_cli_jobs_default():
    base = parse_run_request_json({"filelist": "top.f", "jobs": 16})
    ap = _build_parser()
    args = ap.parse_args(["run.json"])
    cli = run_config_from_args(args)
    merged = merge_run_config(base, cli, args)
    assert merged.jobs == 16


def test_merge_keeps_json_filelist_when_positional_is_run_json(tmp_path: Path):
    fl = tmp_path / "top.f"
    fl.write_text("/dummy.v\n", encoding="utf-8")
    run_json = tmp_path / "run.json"
    run_json.write_text('{"filelist": "top.f", "jobs": 16}', encoding="utf-8")
    base = load_run_request(run_json)
    ap = _build_parser()
    args = ap.parse_args([str(run_json)])
    cli = run_config_from_args(args)
    merged = merge_run_config(base, cli, args)
    assert merged.filelist == str(fl.resolve())


def test_merge_cli_jobs_overrides_json():
    base = parse_run_request_json({"filelist": "top.f", "jobs": 16})
    ap = _build_parser()
    args = ap.parse_args(["run.json", "-j", "4"])
    cli = run_config_from_args(args)
    merged = merge_run_config(base, cli, args)
    assert merged.jobs == 4


def test_auto_detect_run_json_without_config_flag(tmp_path: Path):
    fl = tmp_path / "top.f"
    fl.write_text("/dummy.v\n", encoding="utf-8")
    run_json = tmp_path / "run.json"
    run_json.write_text(
        '{"filelist": "top.f", "jobs": 16}',
        encoding="utf-8",
    )
    loaded = try_load_run_request_from_path(run_json)
    assert loaded is not None
    path, cfg, jobs_src = loaded
    assert path == run_json
    assert jobs_src == "jobs"
    assert cfg.jobs == 16
    assert cfg.filelist == str(fl.resolve())


def test_jobs_nested_under_connect_block():
    cfg = parse_run_request_json(
        {
            "filelist": "top.f",
            "connect": {"jobs": 16, "checks": [{"id": "a", "a": "t.a", "b": "t.b"}]},
        },
        base_dir="/tmp",
    )
    assert cfg.jobs == 16


def test_jobs_case_insensitive_key():
    cfg = parse_run_request_json(
        {"filelist": "top.f", "Jobs": 16},
        base_dir="/tmp",
    )
    assert cfg.jobs == 16


def test_jobs_workers_alias():
    cfg = parse_run_request_json(
        {"filelist": "top.f", "workers": 12},
        base_dir="/tmp",
    )
    assert cfg.jobs == 12


def test_path_walk_mode_from_batch_overrides_run_config(tmp_path: Path):
    fl = tmp_path / "design.f"
    fl.write_text("/dummy.v\n", encoding="utf-8")
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps(
            {
                "mode": "path-walk",
                "top": "top",
                "checks": [{"id": "a", "a": "top.a", "b": "top.b"}],
            }
        ),
        encoding="utf-8",
    )
    run_json = tmp_path / "run.json"
    run_json.write_text(
        json.dumps(
            {
                "filelist": "design.f",
                "top": "top",
                "check_connect_batch": "batch.json",
            }
        ),
        encoding="utf-8",
    )
    from hierwalk.run_request import load_run_request, merge_run_config

    base = load_run_request(run_json)
    assert base.mode == "check-connect-batch"
    ap = _build_parser()
    args = ap.parse_args([str(run_json)])
    cli = run_config_from_args(args)
    merged = merge_run_config(base, cli, args)
    final, _src, _, _ = merge_options_from_connect_batch_json(merged, batch, args)
    assert final.mode == "path-walk"


def test_filelist_from_check_connect_batch_json(tmp_path: Path):
    fl = tmp_path / "design.f"
    fl.write_text("/dummy.v\n", encoding="utf-8")
    batch = tmp_path / "checks.json"
    batch.write_text(
        json.dumps(
            {
                "filelist": "design.f",
                "top": "top",
                "checks": [{"id": "a", "a": "top.a", "b": "top.b"}],
            }
        ),
        encoding="utf-8",
    )
    ap = _build_parser()
    args = ap.parse_args(["--check-connect-batch", str(batch)])
    cli = run_config_from_args(args)
    assert cli.filelist == ""
    merged, _src, _, _ = merge_options_from_connect_batch_json(cli, batch, args)
    assert merged.filelist == str(fl.resolve())
    assert merged.top == "top"


def test_jobs_from_check_connect_batch_json(tmp_path: Path):
    batch = tmp_path / "checks.json"
    batch.write_text(
        """
        {
          "top": "SOC_TOP",
          "jobs": 16,
          "ignore-path": ["pcielinktop"],
          "checks": [{"id": "a", "a": "top.a", "b": "top.b"}]
        }
        """,
        encoding="utf-8",
    )
    ap = _build_parser()
    args = ap.parse_args(
        ["top.f", "--check-connect-batch", str(batch)],
    )
    cli = run_config_from_args(args)
    merged, src, _, _ = merge_options_from_connect_batch_json(cli, batch, args)
    assert merged.jobs == 16
    assert src == "connect-batch:jobs"
    assert merged.ignore_path == ("pcielinktop",)
    assert merged.top == "SOC_TOP"