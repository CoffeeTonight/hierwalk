"""hg_core run JSON env / index-cwd parity with hier-walk."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hg_core.run_config import (
    HgRunConfig,
    load_hg_run_config,
    parse_hg_run_config,
    require_hg_run_config,
)


def test_apply_env_from_checks_json(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HCH_INDEX_CWD", raising=False)
    doc_path = tmp_path / "run.json"
    doc_path.write_text(
        json.dumps(
            {
                "filelist": "fl.f",
                "top": "top",
                "env": {"HCH_INDEX_CWD": str(tmp_path / "eda")},
                "checks": [{"id": "hg1", "a": "top.u.out", "b": "top.u.out"}],
            }
        ),
        encoding="utf-8",
    )
    cfg = load_hg_run_config(doc_path)
    assert "HCH_INDEX_CWD" in cfg.env_applied
    assert os.environ["HCH_INDEX_CWD"] == str(tmp_path / "eda")
    assert len(cfg.checks) == 1
    assert cfg.checks[0].check_id == "hg1"


def test_run_conn_check_nested_checks(tmp_path: Path):
    doc_path = tmp_path / "RUN.json"
    doc_path.write_text(
        json.dumps(
            {
                "filelist": "fl.f",
                "top": "top",
                "run_conn_check": {
                    "enable": 1,
                    "checks": [{"id": "c1", "a": "top.a", "b": "top.b"}],
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = load_hg_run_config(doc_path)
    assert len(cfg.checks) == 1
    assert cfg.checks[0].endpoint_a == "top.a"


def test_index_cwd_resolved_relative_to_json_dir(tmp_path: Path):
    cfg = parse_hg_run_config(
        {"filelist": "fl.f", "top": "top", "index-cwd": "subdir"},
        tmp_path,
    )
    assert cfg.index_cwd == str((tmp_path / "subdir").resolve())


def test_cli_overrides_json_top_and_filelist(tmp_path: Path):
    cfg = parse_hg_run_config(
        {"filelist": "from.json", "top": "from_top", "index-cwd": "."},
        tmp_path,
        filelist_cli=str(tmp_path / "cli.f"),
        top_cli="cli_top",
        apply_env=False,
    )
    assert cfg.filelist == str(tmp_path / "cli.f")
    assert cfg.top == "cli_top"


def test_require_top_and_filelist():
    with pytest.raises(SystemExit, match="missing top"):
        require_hg_run_config(HgRunConfig(filelist="fl.f", top=""))
    with pytest.raises(SystemExit, match="missing filelist"):
        require_hg_run_config(HgRunConfig(filelist="", top="top"))