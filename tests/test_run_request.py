"""Run configuration JSON parsing and CLI RUN.json positional."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hierwalk.run_request import (
    apply_config_env_from_document,
    load_run_request,
    parse_run_request_json,
    resolve_connectivity_request,
)
from hierwalk.stress_gen import (
    STANDARD_CONFIG,
    build_stress_run_config,
    generate_stress_design,
    write_stress_artifacts,
)


def test_parse_run_request_inline_connect(tmp_path: Path):
    cfg = parse_run_request_json(
        {
            "filelist": "design.f",
            "top": "top",
            "mode": "check-connect-batch",
            "defines": ["USE_X=1", "DEBUG"],
            "include_ff": True,
            "connect": {
                "checks": [{"id": "a", "a": "top.s", "b": "top.d"}],
            },
        },
        base_dir=tmp_path,
    )
    assert cfg.top == "top"
    assert cfg.defines_map == {"USE_X": "1", "DEBUG": "1"}
    assert cfg.include_ff
    req = resolve_connectivity_request(cfg)
    assert req is not None
    assert req.checks[0].check_id == "a"
    assert req.include_ff


def test_load_run_request_resolves_relative_paths(tmp_path: Path):
    fl = tmp_path / "design.f"
    fl.write_text("/dummy.v\n", encoding="utf-8")
    cfg_path = tmp_path / "run.json"
    cfg_path.write_text(
        json.dumps(
            {
                "filelist": "design.f",
                "top": "top",
                "check_connect_batch": "checks.json",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "checks.json").write_text(
        json.dumps({"checks": [["top.a", "top.b"]]}),
        encoding="utf-8",
    )
    cfg = load_run_request(cfg_path)
    assert cfg.filelist == str(fl.resolve())
    assert cfg.check_connect_batch == str((tmp_path / "checks.json").resolve())


def test_apply_config_env_from_document(monkeypatch):
    monkeypatch.delenv("HIERWALK_LOG_SLOW_FILES", raising=False)
    monkeypatch.setenv("HIERWALK_JOBS", "4")
    applied = apply_config_env_from_document(
        {
            "env": {
                "HIERWALK_INCLUDE_WARM": "1",
                "HIERWALK_INCLUDE_WARM_MAX": "0",
                "HIERWALK_LOG_SLOW_FILES": "1",
                "HIERWALK_JOBS": "16",
            }
        }
    )
    assert "HIERWALK_INCLUDE_WARM" in applied
    assert "HIERWALK_LOG_SLOW_FILES" in applied
    assert "HIERWALK_JOBS" in applied
    import os

    assert os.environ["HIERWALK_INCLUDE_WARM"] == "1"
    assert os.environ["HIERWALK_INCLUDE_WARM_MAX"] == "0"
    assert os.environ["HIERWALK_JOBS"] == "16"


def test_cli_config_only_stress_run(tmp_path: Path):
    design = generate_stress_design(seed=42, depth=10, branch_factor=5, config=STANDARD_CONFIG)
    out = tmp_path / "stress"
    paths = write_stress_artifacts(design, out)
    run_json = Path(paths["run.json"])
    cfg = load_run_request(run_json)
    assert cfg.filelist == str((out / "filelist.f").resolve())
    assert cfg.connect_inline is not None

    proc = subprocess.run(
        ["hier-walk", str(run_json)],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip() and not ln.startswith("#")]
    by_id = {ln.split("\t", 1)[0]: ln for ln in lines[1:]}
    assert "True" in by_id["port_port"]
    assert "False" in by_id["missing_hierarchy"]


def test_cli_help_config_and_connect():
    for flag, needle in (
        ("--help-config", "run JSON"),
        ("--help-connect", "connectivity batch JSON"),
        ("--help-stress", "stress_gen"),
    ):
        proc = subprocess.run(
            ["hier-walk", flag],
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc.returncode == 0
        assert needle.lower() in proc.stdout.lower()


def test_cli_rejects_removed_config_flag():
    proc = subprocess.run(
        ["hier-walk", "--config", "run.json"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "unrecognized arguments: --config" in proc.stderr


def test_cli_help_lists_config_flags():
    proc = subprocess.run(
        ["hier-walk", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "RUN.json" in proc.stdout
    assert "--help-config" in proc.stdout
    assert "--help-connect" in proc.stdout
    assert "--help-stress" in proc.stdout
    assert "connectivity mode:" in proc.stdout


def test_stress_run_config_matches_connect_request():
    design = generate_stress_design(seed=7, depth=8, config=STANDARD_CONFIG)
    run_cfg = build_stress_run_config(design)
    req = resolve_connectivity_request(run_cfg)
    assert req is not None
    assert req.top == design.top
    assert len(req.checks) == 4