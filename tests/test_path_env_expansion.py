"""Environment variable expansion for JSON paths and filelist RTL lines."""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hierwalk.filelist import parse_filelist
from hierwalk.hch_compat.filelist_preprocess import expand_filelist
from hierwalk.hch_compat.platform_paths import expand_path_vars, merge_environ
from hierwalk.run_request import _resolve_path, parse_shared_run_request_json


def _libc_setenv(name: str, value: str) -> None:
    libc = ctypes.CDLL(ctypes.util.find_library("c"))
    libc.setenv.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    if libc.setenv(name.encode(), value.encode(), 1) != 0:
        raise OSError(f"setenv({name!r}) failed")


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


def test_merge_environ_sees_libc_setenv_without_os_environ_update(tmp_path: Path):
    """C host setenv after Python start — libc environ must be visible."""
    key = "HWALK_LIBC_ONLY"
    value = str(tmp_path / "from_libc")
    _libc_setenv(key, value)
    assert os.environ.get(key) is None
    assert merge_environ().get(key) == value
    expanded = expand_path_vars(f"${key}/design.f")
    assert expanded == f"{value}/design.f"


def test_inprocess_libc_setenv_json_filelist_and_rtl(tmp_path: Path):
    rtl = tmp_path / "rtl"
    rtl.mkdir()
    (rtl / "top.v").write_text("module top(input a, output z); assign z=a; endmodule\n", encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text("$HWALK_RTL_DIR/top.v\n", encoding="utf-8")
    _libc_setenv("HWALK_RTL_DIR", str(rtl))
    _libc_setenv("HWALK_FL", str(fl))

    cfg = parse_shared_run_request_json(
        {"filelist": "$HWALK_FL", "top": "top"},
        base_dir=tmp_path,
    )
    assert cfg.filelist == str(fl.resolve())

    parsed = expand_filelist(cfg.filelist)
    assert len(parsed.source_files) == 1
    assert parsed.source_files[0].resolve() == (rtl / "top.v").resolve()


def test_emit_filelist_failure_logs_unset_env_and_missing_file(tmp_path: Path, capsys):
    from hierwalk.filelist import emit_filelist_failure, parse_filelist

    fl = parse_filelist(
        "$HWALK_TOTALLY_MISSING_FLIST/design.f",
        on_progress=lambda _msg: None,
        defer_source_exists=False,
    )
    emit_filelist_failure(
        fl,
        config_filelist="$HWALK_TOTALLY_MISSING_FLIST/design.f",
        stream=sys.stderr,
    )
    err = capsys.readouterr().err
    assert "run: filelist: FAIL" in err
    assert "unset env var $HWALK_TOTALLY_MISSING_FLIST" in err
    assert "resolved top .f path:" in err


def test_emit_filelist_failure_logs_missing_rtl_source(tmp_path: Path, capsys):
    fl_path = tmp_path / "list.f"
    fl_path.write_text("missing_child.v\n", encoding="utf-8")
    from hierwalk.filelist import emit_filelist_failure, parse_filelist

    fl = parse_filelist(
        str(fl_path),
        on_progress=lambda _msg: None,
        defer_source_exists=False,
    )
    emit_filelist_failure(fl, config_filelist=str(fl_path), stream=sys.stderr)
    err = capsys.readouterr().err
    assert "Source not found:" in err
    assert "missing_child.v" in err


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
    assert "filelist: expanding" in proc.stderr
    assert str(fl.resolve()) in proc.stderr