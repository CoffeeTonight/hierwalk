"""Environment variable expansion for JSON paths and filelist RTL lines."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hierwalk.filelist import parse_filelist
from hierwalk.hch_compat.filelist_preprocess import expand_filelist
from hierwalk.hch_compat.platform_paths import expand_path_vars
from hierwalk.run_request import _resolve_path, parse_shared_run_request_json


def test_expand_path_vars_uses_process_environ(monkeypatch):
    monkeypatch.setenv("HWALK_PROJ", "/proj/acme")
    assert expand_path_vars("$HWALK_PROJ/rtl/design.f") == "/proj/acme/rtl/design.f"
    assert expand_path_vars("${HWALK_PROJ}/rtl/design.f") == "/proj/acme/rtl/design.f"


def test_resolve_path_expands_env_for_json_filelist(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HWALK_FL_ROOT", str(tmp_path))
    fl = tmp_path / "top.f"
    fl.write_text("child.v\n", encoding="utf-8")
    (tmp_path / "child.v").write_text("module child; endmodule\n", encoding="utf-8")
    resolved = _resolve_path(tmp_path / "cfg", "$HWALK_FL_ROOT/top.f")
    assert resolved == str(fl.resolve())


def test_parse_shared_run_request_json_filelist_expands_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HWALK_FL_ROOT", str(tmp_path))
    fl = tmp_path / "design.f"
    fl.write_text("top.v\n", encoding="utf-8")
    doc = {"filelist": "$HWALK_FL_ROOT/design.f", "top": "top"}
    cfg = parse_shared_run_request_json(doc, base_dir=tmp_path)
    assert cfg.filelist == str(fl.resolve())


def test_expand_filelist_top_path_and_rtl_lines_use_environ(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HWALK_RTL_DIR", str(tmp_path / "rtl"))
    rtl_dir = tmp_path / "rtl"
    rtl_dir.mkdir()
    (rtl_dir / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
    fl = tmp_path / "list.f"
    fl.write_text("$HWALK_RTL_DIR/top.v\n", encoding="utf-8")
    monkeypatch.setenv("HWALK_FL", str(fl))
    result = expand_filelist("$HWALK_FL")
    assert len(result.source_files) == 1
    assert result.source_files[0].resolve() == (rtl_dir / "top.v").resolve()


def test_cli_run_json_filelist_env_expansion(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HWALK_RTL_DIR", str(tmp_path / "rtl"))
    rtl_dir = tmp_path / "rtl"
    rtl_dir.mkdir()
    (rtl_dir / "top.v").write_text("module top(input a, output z); assign z=a; endmodule\n", encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text("$HWALK_RTL_DIR/top.v\n", encoding="utf-8")
    monkeypatch.setenv("HWALK_FL", str(fl))
    run_json = tmp_path / "run.json"
    run_json.write_text(
        json.dumps(
            {
                "filelist": "$HWALK_FL",
                "top": "top",
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
    env = os.environ.copy()
    src = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = str(src)
    proc = subprocess.run(
        [sys.executable, "-m", "hierwalk", str(run_json)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "test-suite" in proc.stderr
    assert "No sources in filelist" not in proc.stderr
    assert f"filelist: expanding {fl.name}" in proc.stderr